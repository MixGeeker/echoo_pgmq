# 协议、语义与兼容矩阵

## 支持范围

| 项目 | 0.1.0 范围 |
|---|---|
| 传输 | TCP + TLS 1.2/1.3，双向证书验证；无明文 |
| 协议 | AMQP 1.0；不支持 AMQP 0-9-1、MQTT、STOMP、Kafka |
| 服务端实现 | PostgreSQL background worker + Qpid Proton C 0.40.0 OpenSSL backend |
| 认证 | 经验证客户端证书的 CN → PostgreSQL 角色 → 显式队列 ACL |
| SASL | ANONYMOUS/EXTERNAL 协商或直接 AMQP；不以 SASL 自报身份授权 |
| 地址 | 已存在队列的精确名称，如 `erp/orders`；不存在即拒绝，不动态创建 |
| 节点 | 持久 work queue，竞争消费；不提供 exchange/topic fanout/binding |
| 消息 | 完整 encoded AMQP message 无损 bytea 存储，不转成 JSONB |
| 生产 | unsettled transfer，数据库提交后 Accepted；pre-settled 不受支持 |
| 消费 | 显式 terminal outcome，带领取代次；accepted/released/rejected/modified |
| link resumption | 不支持 durable/resumable link；重连建立新 link |
| 过滤/selector | 不支持；不静默忽略请求 |
| 动态节点 | 不支持 |
| AMQP 事务 | 不支持 coordinator/declare/discharge/transactional-state |
| exactly-once | 不保证；业务必须幂等 |
| SQL 原子业务更新 | 支持同一 PostgreSQL 事务中的 SQL 入队 |
| HA/复制 | 依赖部署方 PostgreSQL 配置；首版没有独立 broker HA 协议 |

自动化互操作使用 Python Qpid Proton。其它客户端（Qpid JMS、.NET、JavaScript、Rust 等）必须按实际版本做互操作测试，不能仅凭“支持 AMQP 1.0”宣称完整兼容。RabbitMQ 自有路由地址、管理 API 与0-9-1客户端并不适用。

## 地址与身份

队列名最长128字节，数据库约束要求以字母/数字开始，其后为 ASCII 字母、数字、点、下划线、斜杠或短横线。地址本身不带 URL 编码、`queue://` 前缀或 exchange 路由语义。

CN 必须是短的 ASCII 主体名（最多63字节），并精确匹配被授权数据库角色。客户端即使发送 SASL username、message user-id 或不同 container-id，也不会替换证书身份。SQL API 使用 session_user，SET ROLE 不能模拟另一个已授权登录主体。

## Accepted、租约与重试

1. 生产者发送完整消息，worker 校验资源上限和编码，再执行数据库入队事务
2. 事务同步提交成功后才发 Accepted；数据库失败/容量耗尽/编码非法不得返回 Accepted
3. 若提交成功但网络在 Accepted 到达前断开，生产者无法知道是否已提交。重试可能产生重复；AMQP 首版没有基于 message-id 的自动去重
4. 消费领取会生成 owner + generation + lease_until；正文不改写，因此重投标识不保证反映在 header.delivery-count 中
5. Accepted 删除；Released 和 Modified 按重试策略重新可见；Rejected 留为死信。首版 Modified 不实现复杂 annotation/delivery-failed 语义
6. 租约超时后可再领取，旧 generation 的 ACK 无效。过期 link 会关闭以释放 receipt 状态，客户端需处理 link detach 并重连
7. 消费者业务处理成功后、ACK 提交前中断会导致重复处理。使用业务唯一键、收件表或事务性幂等记录抵御重复

SQL 的可选幂等 key 是有容量与过期窗口的入队去重账本，不是无限期 exactly-once 保证。相同 key 重试返回最初 message id；账本可能在 ACK 后仍存在直到清理/到期。详见存储文档。

## 协议边界与资源限制

当前固定最大 frame 64KiB，单 session 入站窗口128KiB、每连接最多8个 session；消息可以跨 frame，但完整编码仍受 max_message_bytes 限制。连接、link、inflight、buffer 预算与握手/空闲超时见运维手册。AMQP credit 为流控，不允许靠无界缓存吸收超额生产。

SQL专家原始接口写入的空消息、非法编码或超worker上限消息会在领取事务中直接保留为死信；单次最多扫描8条毒消息后让出执行，不应永久占据队头。

不支持的行为应得到明确关闭/拒绝；客户端必须处理 transport close、link detach、Rejected、超时与重连。连接丢失不是成功确认的替代证据。

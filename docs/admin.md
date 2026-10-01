# 运维手册

## 上线前清单

- 在与门店相同 OS、PG 主版本、存储设备、服务身份的隔离环境完成验收
- 专用证书CA与最小ACL；限制网络来源，监听公网不在默认部署建议中
- 明确至少一次业务处理的幂等方案，以及 Accepted 丢失时生产者的重试策略
- 确认 fsync/full_page_writes/synchronous_commit，检查 SSD/文件系统实际刷盘保证
- 验证数据备份与完整恢复；保留原生库、SQL版本、角色/权限和外部证书配置
- 设置磁盘、WAL、队列容量、死信、worker反复重启、等待锁与消息滞留告警
- 按对应的[v0.1.1发布说明](releases/v0.1.1.md)或[v0.1.0历史说明](releases/v0.1.0.md)核对来源与散列；正式Release原样分发未签名的candidate ZIP，不能当作生产资格证明。项目已采用标准Apache-2.0；生产支持责任仍需明确

## 配置参考

全部 `echoo_pgmq.*` 参数当前为 `PGC_POSTMASTER`，修改后需重启，reload不足以应用。

| 参数 | 默认 | 说明 |
|---|---:|---|
| enabled | off | 未主动启用时不注册 listener |
| database | postgres | 安装扩展的数据库 |
| role | echoo_pgmq_worker | 专用受限角色 |
| listen_address | 127.0.0.1 | 数字地址；默认本机 |
| port | 5671 | 独立 AMQP TLS端口 |
| tls_certificate / tls_private_key / tls_ca_file | 空 | 必需的PEM文件；不存在或无效则失败关闭 |
| max_connections | Linux64 / Windows32 | Windows最高60，受原生wait handle限制 |
| max_links_per_connection | 16 | 每连接link上限 |
| max_message_bytes | 1048576 | 完整AMQP编码字节数，上限16MiB |
| max_inflight_per_link | 32 | 每消费link未确认投递数 |
| max_buffer_bytes | 67108864 | 全局应用/排队输出缓冲预算；不等同于进程总RSS上限 |
| visibility_seconds | 60 | 消费租约，1..86400秒 |
| poll_interval_ms | 50 | 有credit且暂时无消息的轮询间隔 |
| idle_timeout_ms | 30000 | 初始TLS握手/AMQP空闲超时 |
| statement_timeout_ms | 5000 | 每次队列SQL操作的最长时间 |

内部固定最大frame64KiB、session入站容量128KiB、每连接8session；SQL lock_timeout为1000ms。参数提高会增加内存、锁竞争及故障影响；不要仅靠增大上限解决背压。

## 健康与容量

管理员可检查：

```sql
SELECT pid, backend_type, state, wait_event_type, wait_event
FROM pg_stat_activity WHERE backend_type='echoo_pgmq AMQP listener';
SELECT name, message_count, total_bytes, max_messages, max_bytes FROM echoo_pgmq.queues;
SELECT q.name, m.state, count(*), min(m.created_at)
FROM echoo_pgmq.messages m JOIN echoo_pgmq.queues q USING(queue_id)
GROUP BY q.name,m.state;
SELECT * FROM echoo_pgmq.limits;
```

0.1.1的 `queue_stats` 视图提供按登录主体ACL过滤的队列计数。0.1.0没有此视图。只暴露必要指标，不为了监控给普通客户端底表权限。

计数包含 ready/inflight/dead 的保留消息；total_bytes指消息bytea有效载荷，不包含表/索引/WAL/空洞的磁盘开销。用 pg_total_relation_size 与磁盘监控另行计算真实占用。死信不会自动无限扩容或自动删除，管理员需要有审计的重试/清理策略。

`purge_dead(queue,limit)` 有明确批量上限；`retry_dead(queue,id)` 为管理员操作。任何删除/重试都应先确认业务幂等与审计要求。不要直接更新messages/queues/limits底表，否则可能破坏计数、代次与外键契约。

## PostgreSQL维护

普通logged表产生WAL与死元组。保留autovacuum，不要为跑分关掉fsync/同步提交，不要把表改成UNLOGGED。按实际写入/删除负载观察vacuum、WAL、checkpoint与磁盘延迟。单例全局配额行和每队列配额行的锁是已知吞吐约束，相关调优必须重新跑并发容量测试。

SQL enqueue可以和业务更新同事务，但调用方自行承担事务长度和持久化设置。全局配额singleton行锁会一直持有到外层ERP事务结束，因此队列A的长事务也可能阻塞队列B的AMQP入队，直到1000ms锁超时导致Rejected；事务释放后可重试。不要通过关闭同步提交或绕过配额锁掩盖这个取舍；长事务会影响清理并占用锁。worker自己的每次操作是短事务，SQL失败被捕获并转为协议失败；原生段错误仍可能触发整个集群恢复。

## 备份与恢复

物理备份/PITR采用标准PostgreSQL流程，业务与队列必须同一恢复点。逻辑备份已登记扩展配置表与身份序列，使pg_dump/restore包含消息、队列ACL、幂等账本、租约及序列状态。角色定义仍需单独备份/恢复；扩展库与对应SQL版本必须先安装到目标实例。特别注意：pg_dump 的 CREATE EXTENSION 不指定源版本，会使用目标control默认版本。恢复前须查询源pg_extension.extversion，在目标空数据库显式 CREATE EXTENSION echoo_pgmq VERSION '源版本'，再导入备份；恢复后核对extversion以及queue_stats等版本特有接口，不能只看消息数据。

不要只复制PGDATA而忽略WAL一致性。不要先恢复ERP再用不同时间的消息备份“拼起来”。恢复后旧消费者必须断开，在途租约超时后重投；若外部副作用已执行，应依赖业务去重避免二次影响。验收必须比较正文、计数、ACL、idempotency结果与下一次生成id。

## 常见故障

- listener没起来：检查shared_preload_libraries、enabled、目标数据库、角色、证书权限、Proton/OpenSSL动态库与端口冲突
- PG16 worker登录被拒：内部角色需LOGIN且无密码，同时pg_hba显式拒绝外部该角色；PG17/18可NOLOGIN
- TLS握手失败：检查双向CA、证书有效期/EKU、客户端服务器名与SAN，避免为排障关闭验证
- Unauthorized/link refused：队列必须存在，CN角色必须有显式ACL；重新创建同名角色后旧授权不应自动继承
- Rejected/容量错误：看SQLSTATE与队列/全局容量、消息编码/尺寸、死信占用；不要盲目重试形成风暴
- 消息重复：可能是租约超时、ACK丢失或提交后Accepted丢失；检查业务幂等与处理时长
- worker连续重启：暂时关闭流量，保存脱敏日志/版本/最小重现；必要时禁用扩展listener并重启。不要直接丢弃数据

## 断电与硬件

仓库保留SIGKILL/taskkill与immediate restart测试，但当前普通CI不执行这些场景，完整安全/故障资格仍为blocked_security_review。不能把保留的测试代码或历史覆盖当作本次二进制已通过的结果；即便执行进程强杀，也没有切断设备电源，不能证明磁盘控制器缓存、掉电保护或文件系统屏障行为。真实门店验收应在可牺牲硬件/数据上、经过授权的流程中断电，恢复后核对已Accepted记录、未提交事务和业务副作用；同时测试UPS与磁盘满场景。


## 分发版本与SQL扩展版本

v0.1.1原生分发不会自动迁移数据库。MANIFEST.version/distribution_version/native_build_version
表示原生分发0.1.1，extensionVersion/sql_default_version仍为0.1.0；可选SQL0.1.1
只能显式升级。已有数据库实际版本以pg_extension.extversion为准，不能由Release tag推断。

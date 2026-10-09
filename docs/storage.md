# 事务存储与 SQL API

主要目标为 PostgreSQL 18，另兼容验证 PostgreSQL 16/17。正文、投递凭据、ACL、幂等记录与计数器全部保存在普通的 **PostgreSQL 持久表（logged table）** 中。没有额外的分段文件、自定义 WAL 或第二套持久化域。`bytea` 保存完整的 AMQP 1.0 编码消息，不转换为 JSON 或文本；PostgreSQL TOAST 可以透明压缩其物理表示。传输帧和会话帧属于传输状态，不属于 AMQP 消息正文。

## 持久化与投递契约

- 入队属于调用方的 PostgreSQL 事务。业务更新与 `enqueue` 一起提交具有原子性；回滚会同时撤销两者
- 网络工作进程必须以 `synchronous_commit=on` 成功提交 `publish`，之后才能发送 AMQP `Accepted` 确认。确认到达前连接丢失，会使发布结果不确定；生产者重试可能产生重复消息
- 领取消息时，先提交递增的代次、随机连接 owner UUID 与租约截止时间，再由工作进程发送消息。完整投递凭据为 `(queue, id, generation, owner)`，消息 ID 与凭据代次不可互相替代
- ACK、release 和 reject 只能作用于当前、未过期、处于 inflight 状态的凭据。错误 owner、旧代次、重复 ACK 或过期 ACK 返回 `false`。即使尚未被重新领取，过期凭据也不能确认消息
- 投递语义是**至少一次**。可见性租约过期或进程崩溃可能导致重投；消费者必须保证业务副作用幂等。仅靠 PostgreSQL 事务不能让外部副作用变成恰好一次
- 仍需满足 PostgreSQL 的常规持久化前提：`fsync=on`、`full_page_writes=on`、实际遵守刷盘请求的可靠存储以及备份。`synchronous_commit=on` 表示按主库配置执行提交持久化，并不自动提供跨区域复制。网络工作进程崩溃测试和数据库进程重启测试不能证明门店磁盘的真实断电行为
- SQL 调用者可以独立选择不安全的提交配置。SQL 函数不会悄悄覆盖调用者事务的持久化策略

## 安装与信任边界

由受控管理员角色安装扩展，其对象归安装者所有。扩展模式不可迁移。`PUBLIC` 只具有模式使用权和明确列出的公共 API 执行权，不具有底表、序列、工作进程辅助函数或管理函数的权限。

每个 `SECURITY DEFINER` 函数将搜索路径固定为 `pg_catalog, echoo_pgmq, pg_temp`，并显式限定存储对象所属模式。普通 SQL 包装函数使用 `session_user`，不接受调用者指定的身份，也不采用 definer 函数内部的 `current_user`。`SET ROLE` 不能冒充另一个队列主体。因此，共用数据库登录身份的连接池也会共用同一队列主体；需要不同信任边界时，应使用不同的已认证登录身份。

以扩展所有者身份执行的示例：

```sql
CREATE ROLE store_042 LOGIN;
CREATE ROLE echoo_pgmq_worker NOLOGIN;
CREATE EXTENSION echoo_pgmq;
SELECT echoo_pgmq.create_queue('store/042/orders');
SELECT echoo_pgmq.grant_queue('store/042/orders', 'store_042', true, true);
GRANT EXECUTE ON FUNCTION
    echoo_pgmq.publish(text,bytea,text),
    echoo_pgmq.claim(text,text,uuid,integer),
    echoo_pgmq.settle(text,bigint,bigint,uuid,text,text),
    echoo_pgmq.authorize(text,text,text)
TO echoo_pgmq_worker;
```

此处 `NOLOGIN` 适用于 PostgreSQL 17/18。PostgreSQL 16 的工作进程角色需使用无密码的 `LOGIN`，并在 `pg_hba.conf` 最前面显式拒绝其外部连接；完整配置见 [快速开始](quickstart.md)。

工作进程角色是**受信任的身份网关**。由于证书认证发生在原生 AMQP 工作进程中，其四个私有函数接受显式主体参数。工作进程必须使用配置的 CA 验证客户端证书，再从已认证证书提取精确身份；AMQP 消息、地址或属性绝不能充当身份来源。该角色可以代表 ACL 中的主体执行操作，不能与不可信 SQL 客户端共用，也不能向业务用户授予其成员资格。

ACL 中的角色名必须已在 PostgreSQL 中存在；角色成员关系不会隐式继承队列 ACL。撤销权限影响之后的 SQL 操作，包括投递结算。ACL 同时绑定主体名称和 PostgreSQL 角色身份（`regrole`）。删除或重命名角色后，后续授权立即失效；重新创建同名角色不会恢复旧授权，管理员必须显式重新授权。PostgreSQL 将 `regrole` 按角色名导出，以便跨集群恢复时正确解析角色；恢复扩展数据之前应先建立预期角色。

`create_queue`、`grant_queue`、`revoke_queue`、`purge_dead`、`retry_dead` 和 SQL 0.1.2 起的 `drop_queue` 默认仅管理员可执行。公共函数不会隐式创建队列。安装者可以按需显式委派特定管理函数的执行权。

## 公共 SQL 接口

| 函数 | 返回值与行为 |
| --- | --- |
| `message_binary(body bytea)` | 将业务字节编码为持久化 AMQP 1.0 data-section 消息，内容类型为 `application/octet-stream` |
| `enqueue_binary(queue text, body bytea, idempotency_key text DEFAULT NULL)` | 先使用 `message_binary` 编码任意业务字节，再原子入队 |
| `enqueue(queue text, body bytea, idempotency_key text DEFAULT NULL)` | 返回 `bigint` 消息 ID；需要生产 ACL |
| `read(queue text, owner uuid, visibility_seconds int DEFAULT 30)` | 返回零行或一行 `(id bigint, generation bigint, body bytea)`；需要消费 ACL |
| `ack(queue text, id bigint, generation bigint, owner uuid)` | 返回 `boolean`；有效当前凭据会删除正文并释放容量 |
| `release(queue text, id bigint, generation bigint, owner uuid)` | 返回 `boolean`；有效当前凭据会立即重试，达到尝试次数上限时除外 |
| `reject(queue text, id bigint, generation bigint, owner uuid)` | 返回 `boolean`；有效当前凭据会使消息变为保留的死信 |

可见性租约必须为 1–86,400 秒。每个独立消费者会话应生成新的 owner UUID。每次投递都应保存领取时返回的 generation，不能查询最新代次来结算旧投递。SQL 存储层有意不解析 AMQP，因此允许空 `bytea`；网络客户端必须发送有效的 AMQP 编码消息。

```sql
BEGIN;
UPDATE orders SET state = 'confirmed' WHERE id = 123;
SELECT echoo_pgmq.enqueue_binary(
    'store/042/orders', $1::bytea, 'order:123:confirmed:v1');
COMMIT;
```

`$1` 表示由驱动绑定的业务字节参数，不是 SQL 字符串插值。需要通过 AMQP 消费时，只有已经完成 AMQP 编码的消息才应直接使用 `enqueue`。二进制辅助函数的固定输入上限为 16 MiB 减去 256 字节；队列限制作用于编码结果，包括消息头和属性的开销。网络工作进程的所有 SPI 调用都必须使用参数传递队列名、身份、正文、消息 ID、代次与 owner UUID。

0.1.1 提供 `echoo_pgmq.queue_stats` 视图，仅展示向 `session_user` 授予了任意队列 ACL 的队列，其内容包括保留消息/字节计数与配置上限。没有 ACL 的超级用户会话在该视图中也看不到记录，可改为以管理员方式查询底表。死信计入保留总量。

## 原生工作进程私有接口

以下签名构成首版原生工作进程的稳定桥接接口：

```text
publish(queue text, body bytea, identity text) -> bigint
claim(queue text, identity text, owner uuid, visibility_seconds integer)
    -> TABLE(id bigint, generation bigint, body bytea)
settle(queue text, id bigint, generation bigint, owner uuid,
       outcome text, identity text) -> boolean
authorize(queue text, identity text, operation text) -> boolean
```

`authorize` 接受 publish/enqueue/produce 或 consume/claim/read/settle 操作名；未知操作、队列不存在或没有权限时返回 false。每次实际操作都会重新核对 ACL，链接建立时的授权只用于提前拒绝。投递结算结果包括 `accepted`、`released`、`rejected` 和 `modified`。`modified` 采用与 `released` 相同的重试策略；此最小队列实现不基于 modified 字段执行 AMQP distribution-mode 过滤。

`claim` 使用 `FOR UPDATE SKIP LOCKED` 锁定一条可领取记录；没有符合条件且未被锁定的消息时不返回记录。它同时会将最多 64 条已耗尽最后一次尝试且租约过期的消息转为死信。可投递记录、耗尽租约和死信分别具有部分索引。领取操作不会锁定全局容量行。

变更容量的操作始终按照全局计数器、队列计数器、消息行的顺序加锁。为了强制执行数据库级容量硬上限，入队与 accepted 结算有意在计数器行上串行化。这是已知吞吐取舍，不能据此作性能承诺。应缩短事务，并为网络工作进程设置锁等待和语句超时。

## 状态机与有毒消息

```text
ready -> inflight                 领取；attempts++、generation++，设置租约
inflight -> ready                 当前 release/modified，且未达到尝试上限
inflight -> deleted               租约到期前的当前 accepted
inflight -> dead                  当前 rejected，或最后一次尝试的 release
过期 inflight -> inflight         仍有剩余尝试次数时再次领取
过期且耗尽尝试 -> dead            后续领取时执行有界清理
 dead -> ready                    管理员 retry_dead；重置 attempts
 dead -> deleted                  管理员 purge_dead
 任意状态 -> deleted              管理员 drop_queue（整队列）
```

死信是原队列消息表中的逻辑状态，没有单独的路由地址。管理员仍可查看其原始正文与原因，正文继续占用已预留容量。`retry_dead(queue, id)` 重置尝试次数并递增代次；`purge_dead(queue, limit DEFAULT 100)` 每次最多删除 10,000 条死信并释放容量。两者都具有事务性。没有定时器自动丢弃消息。耗尽且过期的消息会在之后的领取中转为死信，因此空闲队列的原始管理数据仍可能暂时将其显示为 inflight。

每条消息入队时，将队列 `max_attempts` 保存为其 `attempt_limit`。修改队列配置只影响之后入队的消息；已有消息保留入队时的重试预算，管理员重试后也不改变这一预算。

SQL 原始 `enqueue` 允许任意字节，但原生网络消费路径会检查完整消息编码。非法编码、空正文或超过工作进程上限的存储消息会直接保留为死信，避免反复阻塞后续合法消息；合法消息的未确认投递才按尝试预算重试。

## 容量边界与运维配置

`echoo_pgmq.limits` 包含一条全局计数/配置记录，在创建第一个队列时以事务方式初始化。全新安装会保持数据表为空，以便逻辑恢复直接插入其内容。

- 默认保留消息数量上限：100,000
- 默认保留原始正文字节数上限：1,073,741,824
- 默认单条正文字节数上限：1,048,576

`create_queue(queue, max_messages DEFAULT 10000, max_bytes DEFAULT 67108864, max_message_bytes DEFAULT 1048576, max_attempts DEFAULT 5)` 配置队列边界。全局与队列限制同时生效。只有管理员可以修改 `limits` 或 `queues` 的配置列。不要手动修改计数列或消息、凭据、幂等底表，否则会破坏不变量。

不要将上限降至当前保留总量以下，否则在占用降到新上限之前，新入队会被拒绝。这些限制只统计正文字节，不包含 PostgreSQL 行/索引开销、WAL、TOAST 开销、死元组或备份。应另外监测数据库磁盘使用量，并为 autovacuum、WAL 和磁盘保留余量。确认消息立即释放逻辑容量；物理空间的复用由 PostgreSQL vacuum 控制。高频增删下的表增长需要用真实负载验证。

容量检查具有原子性，使用减法避免加法溢出；超限以 SQLSTATE `54000` 失败，不保留半条消息或部分计数更新。未授权操作以 `42501` 失败。非法边界/参数触发检查约束或 `22023`；未知结算结果触发 `22023`。

可选 SQL 幂等键长度为 1–128 字节，作用域限于单个队列。有效且未过期的键返回最初的消息 ID，即使原消息已经 ACK，也不会比较正文。用同一键提交不同字节属于业务错误，接口仍有意返回原 ID。每队列默认最多保存 10,000 个键，默认窗口为 24 小时，可配置为 1 秒到 7 天。

带键入队会在持有队列锁时清理该键自身的过期记录，以及最多 64 条其它过期记录。账本满时，新带键入队以 `54000` 拒绝。此账本是有界元数据，不是永久恰好一次去重。AMQP message-id 不会隐式启用它；当前原生 publish 接口没有幂等键参数。

队列名长度为 1–128 个 ASCII 字符，需匹配 `^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$`。队列创建属于管理操作，不提供任意租户自行创建队列的入口。模式权限与队列 ACL 必须和消息数据一起备份。

## 模式版本、备份与验证

仓库中的 `0.1.0` 安装脚本是实际实现的初始模式。`0.1.0--0.1.1` 是真实的 `queue_stats` 加法迁移；全新 `0.1.1` 安装包含相同模式及该视图。`ALTER EXTENSION ... UPDATE` 具有事务性，外层事务失败会同时回滚扩展版本和新增对象，保留消息、代次及 owner。此测试覆盖实际初始版本的升级路径，没有虚构旧版模式。目前不提供降级脚本。

数据表已通过 `pg_extension_config_dump` 登记，因此扩展逻辑备份包含配置、ACL、幂等账本和保留消息。集成测试在逻辑导出/恢复后核对正文、ACL 授权访问、去重键、在途凭据、计数器及 identity 序列继续递增。在依赖该功能之前，应使用实际部署的 PostgreSQL 与备份工具版本完成恢复验收。

`pg_dump` 生成的 `CREATE EXTENSION` 不指定源版本，恢复时可能选择目标 control 文件的默认版本。应先查询源库 `pg_extension.extversion`，在目标空数据库显式创建相同版本，再导入备份，最后核对版本专属对象。PostgreSQL 物理备份正常包含全部扩展数据；恢复仍需预先安装兼容的原生扩展与 SQL 脚本，角色和外部证书配置也应单独保留。

以管理员角色对已安装扩展执行 `psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f tests/sql/core.sql`。该脚本回滚测试数据，检查业务/入队回滚、不透明字节保真、去重、私有 API/ACL 隔离、字节/数量容量、当前/旧代次/过期凭据、最大尝试次数死信、搜索路径加固与计数一致性。独立集成测试覆盖并发会话、版本迁移、网络结果与进程重启。功能测试通过不能替代性能实测或生产认证。

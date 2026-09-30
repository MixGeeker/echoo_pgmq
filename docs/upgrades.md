# 升级、失败恢复与回退

## 版本布局

- `0.1.0`：基础队列表、受限 SQL 接口、原生 worker 与 AMQP 1.0
- `0.1.0--0.1.1`：真实的加法迁移，增加按 session_user ACL 过滤的 queue_stats 视图；不重写消息正文
- `0.1.1`：提供相同最终结构的全新安装脚本，用于恢复与回归验证
- `0.1.1--0.1.2`：只替换私有 `_enqueue` 函数的正文，保留函数 OID、ACL 与持久数据
- `0.1.2`：当前 SQL 性能开发候选的全新安装脚本；与顺序升级使用相同 `_enqueue` 正文

默认安装版本为0.1.2，CMake 与候选包版本同步。0.1.0、0.1.1 和原有迁移文件保留不改；历史版本仍可显式安装。全部均为开发候选，未完成生产资格验证。

## 升级前

1. 保存数据库、角色/授权、postgresql.conf、pg_hba.conf 与外部证书配置的可恢复备份，并在隔离环境试恢复
2. 核对新二进制对应 PG 主版本、平台/架构与 Proton/OpenSSL ABI；验证候选 SHA-256
3. 暂停生产者，等待消费者完成/停止领取；记下 ready/inflight/dead 与容量计数
4. 升级原生二进制时停止 PostgreSQL。不要原位截断一个正在被进程映射的 so/DLL；Windows 通常也不允许替换加载中的 DLL
5. 以受信管理员安装新版二进制与所有 SQL 文件，按既定步骤重启并检查 listener 日志

只改 SQL 视图的加法迁移可在事务中执行，但任何原生 ABI 更新都需要按部署窗口重新启动。

## 执行与核验

```sql
BEGIN;
-- 当前为0.1.0时，先执行这一行；已是0.1.1时跳过。
ALTER EXTENSION echoo_pgmq UPDATE TO '0.1.1';
ALTER EXTENSION echoo_pgmq UPDATE TO '0.1.2';
SELECT extversion FROM pg_extension WHERE extname='echoo_pgmq';
COMMIT;
```

用业务登录角色验证 queue_stats 只能看到授权队列；发送一条独立 canary 并消费确认，检查普通业务数据与死信仍在。升级成功以数据库版本与实际 canary 结果为准，不以“安装文件复制完成”为准。

## 失败演练覆盖

自动化测试在隔离数据库中安装0.1.0并写入含 NUL/非UTF-8字节的消息；在同一事务执行升级后注入除零异常，验证：

- extversion 仍为0.1.0
- queue_stats 不存在
- 原始消息正文与 id 不变
- 再次升级成功且数据仍在

这是事务性升级回滚测试，不等于在所有 DDL 阶段断电或磁盘损坏注入。历史完整资格流程包含进程故障用例，但当前普通 allowlist 不运行强杀或 immediate restart；完整资格仍暂停于安全审阅。

0.1.2 新增普通事务演练：在0.1.1写入二进制正文、队列/函数ACL、去重键与在途receipt，显式执行 `BEGIN; ALTER EXTENSION ... UPDATE TO '0.1.2'; ROLLBACK;`，核验旧版本、旧函数正文/ACL和全部数据恢复，再正常升级。另测0.1.0→0.1.1→0.1.2与全新安装函数一致、旧receipt仍可ACK、已消费消息的去重键仍保留。没有新增故障注入。

逻辑恢复必须在目标空库先执行 `CREATE EXTENSION echoo_pgmq VERSION '0.1.2';`，再恢复对应0.1.2源库的逻辑备份；`pg_dump` 的扩展创建语句不携带版本。普通测试核验源/目标 extversion 与 `_enqueue` 正文 SHA-256。历史0.1.1恢复用例继续保留。

## 回退原则

本候选没有自动 DOWN 脚本。不要手工修改 pg_extension.extversion，也不要 DROP EXTENSION 来“回退”，那会删除扩展表及消息。若升级事务尚未提交，ROLLBACK；若已提交，先确认旧二进制与新 SQL 结构兼容，否则从经验证备份恢复到隔离实例，再按运维变更流程切换。恢复点之后的业务与消息需要统一对账。

PG16 → PG17 是 PostgreSQL 主版本升级，不能靠 ALTER EXTENSION 完成。必须使用 PostgreSQL 支持的 pg_upgrade 或逻辑迁移流程，并给两个主版本分别准备扩展库；暂不把 pg_upgrade 纳入已通过的验证范围。

# 已知限制、平台与支持

## 候选状态

这是0.1.2开发候选，未发布生产支持承诺。测试、编译与打包失败会让 CI 失败，不允许用 skip/continue-on-error 把缺失能力伪装为通过。查看所使用提交的 GitHub Actions 日志与候选 MANIFEST，不只看分支徽标。下表保留历史平台证据；这些结果不能视为0.1.2已通过的平台验证。0.1.2的独立结果见[候选说明](sql-reservation-candidate.md)。

| 验证对象 | 说明 |
|---|---|
| Linux PG18（主要目标）及16/17 x86-64 | 152a73c普通两轮各32项+core.sql通过，包括候选归档重装；完整资格未通过 |
| 原生 Windows Server 2022 PG18（主要目标）及16/17 x64 | 原生进程，不经过WSL/Docker；152a73c普通两轮各32项+core.sql通过；不等于Win11验收 |
| Windows 11 实机 | 尚需独立验收；Server2022结果不能替代 |
| PostgreSQL其它主版本/ARM/macOS | 未纳入支持矩阵 |
| 物理断电/电源拔除 | 未验证；SIGKILL/taskkill/immediate shutdown 是进程级故障 |
| 真实 ERP 集成 | 后续共同联调；仓库示例不是业务验收 |
| 生产性能/SLA | 尚无承诺；benchmark记录机器、版本、负载与原始数据 |
| 协议安全 | 有边界/拒绝测试，非全面 AMQP compliance suite 或独立审计 |

## 实现限制

- 单 worker 串行执行短 SQL 操作；队列/全局容量计数采用行级锁，容量强一致也意味着热点写入会竞争
- 同步 SQL 锁等待会暂时延迟该 worker 的其它连接；有 lock_timeout/statement_timeout，但不能等同于完全非阻塞存储
- 普通表+WAL 会产生膨胀与写放大，需要 autovacuum、磁盘与WAL容量管理
- 同一集群只提供一个配置数据库/监听地址；没有在线证书/GUC热重载或多listener管理
- CN签发、ACL维护和证书吊销的运维流程由部署方承担
- 没有 AMQP broker 管理UI、复杂路由、selector、分布式跨节点队列协议
- 没有无限去重或外部系统副作用exactly-once；Accepted丢失与消费者重投是正常恢复场景
- 备份必须把业务数据与队列放在一致恢复点；恢复后在途租约会超时，副作用可能重放

## 提交问题时附带

提交 SHA、artifact SHA-256、OS/架构、PostgreSQL/Proton/OpenSSL/客户端版本、所用配置（去掉私钥路径等敏感细节）、最小复现步骤、具体协议 outcome/SQLSTATE、已脱敏日志和测试命令。说明是否原生 Windows、是否涉及真实断电。性能问题另附硬件、磁盘/WAL配置、并发/消息大小、持续时长、错误率与p50/p95/p99。

不要上传整个 PGDATA、测试 certs 目录、客户资料或真实 ERP 消息。敏感漏洞请走 [私密报告](../SECURITY.md) 路径。

六组合证据与候选下载见[验证记录](validation.md)。所有候选仍为blocked_security_review，不提供生产支持承诺。

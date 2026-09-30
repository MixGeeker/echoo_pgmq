# 0.1.2 SQL 性能开发候选

0.1.2 将已审阅的 NULL 去重键入队原型接入正式 `_enqueue` 安装路径。它是可安装、可升级、可回滚升级事务的开发候选，尚非生产发布；没有测得或承诺产品吞吐、延迟或 WAL 改善。

## 变更范围

NULL 键路径用带容量条件的 `UPDATE ... RETURNING` 预留全局、队列计数；零行匹配必须回到原来的锁定读取，等待未提交的释放或容量调整。只有全局预留成功才尝试队列预留，局部记录恢复为预留前数值，原有检查继续执行，只跳过已经完成的计数 UPDATE。

非 NULL 去重键路径、ACL、全局→队列锁顺序、队列外键所需 NO KEY UPDATE、正文/receipt/ACK、提交与持久化契约保持原实现。C/include 与七个保留 SPI plan 的基线逐字节一致，未增加提前 flush 或额外 wake。历史0.1.0、0.1.1及其迁移文件不改。

新增完整0.1.2安装脚本与0.1.1→0.1.2迁移。迁移使用 `CREATE OR REPLACE FUNCTION`，不扩大 PUBLIC 授权；既有函数 OID 和管理员设置的函数 ACL 保留。默认安装、CMake、候选包名称及 MANIFEST 版本同步为0.1.2。升级和逻辑恢复步骤见[升级说明](upgrades.md)。

## 已验证内容

2026-09-30，隔离 Linux x86-64、PostgreSQL18.6、Apache Proton0.40.0、Node24.19.0/rhea3.0.5，测试钩子关闭，`fsync/full_page_writes/synchronous_commit` 均开启。运行 `scripts/run_integration.py --ordinary --independent-client`，55项通过，0失败/错误/跳过；随后实际执行 `core.sql`，出现 `core SQL tests passed`。正常 fast shutdown 返回0，之后 pg_ctl status返回3且 postmaster.pid不存在。

- 普通 runner 实际按默认版本安装0.1.2，并将 extversion、`_enqueue` 正文 SHA-256及源SQL SHA-256写入 environment.json；不再静默安装0.1.0
- 0.1.0→0.1.1→0.1.2和0.1.1→0.1.2，保留正文、队列/函数ACL、去重账本、在途receipt与容量；函数正文和语义元数据与全新安装一致
- 显式 BEGIN升级、ROLLBACK后核验旧正文与全部数据，再正常升级成功；没有新增故障注入
- 全局已满但另一事务正在ACK释放、队列已满但另一事务正在扩容：确定性观察对持有事务的 transactionid 锁等待，分别覆盖持有事务提交与回滚
- 六连接起跑屏障、不同队列同时入队，分别验证全局消息数与字节配额不会超卖
- 保留历史0.1.1逻辑备份/恢复用例，另测0.1.2；目标先创建精确扩展版本，恢复后核验版本、函数正文、正文/ACL/去重账本/receipt/计数/序列
- 既有原生 AMQP、提交前不发 Accepted、重投/旧ACK、队列容量恢复、隔离角色权限、七计划复用，以及独立 rhea 客户端用例继续通过

首轮新增跨队列测试有2项失败、53项通过：测试在 create_queue初始化全局单例前设置配额，UPDATE影响0行。修正测试初始化顺序，并断言配额 UPDATE确实影响1行；产品 SQL 未改。首轮日志和正常停止记录保留，不能将其计为全通过。最终证据索引见[本地结果](evidence/sql-reservation-0.1.2-local.json)。

## 已知差异与未验证边界

- 只读事务仍拒绝并返回 SQLSTATE25006，但 PostgreSQL主错误文本从 SELECT FOR UPDATE变为 UPDATE，不能宣称诊断文本完全一致
- 更早执行物理UPDATE，管理员自定义触发器/规则或事务内观察者可能看到不同顺序和中间计数；零行UPDATE也可能触发语句级触发器。此类定制未证明等价
- 成功的全局预留使用 UPDATE取得的 NO KEY UPDATE；原路径全局 FOR UPDATE更强，表级锁取得时点也变化。直接管理锁、自定义索引和任意外部事务调度不在等价结论内
- 全局预留后因队列满而拒绝，会产生最终回滚的UPDATE及潜在WAL/清理开销；fallback可能增加工作，不能据此声称普遍提速或减少WAL
- 当前本地普通验证基于默认 READ COMMITTED。更强隔离级别的重试/序列化行为没有专门验证
- 先前原型36项SQL检查仅为独立原型证据；批量SQL CPU机制实验不是MQ产品收益。本候选的原生性能对比已完成，未显示稳定净收益，见[完整报告](performance-sql-reservation.md)

支持范围仍按PG18主要目标、PG16/17兼容目标及现有AMQP协议边界声明；最终head67df046已通过Linux/Windows Server2022×PG18/17/16普通CI与归档重装，共704项；详见[复核报告](performance-sql-reservation.md)。完整安全/长时资格、Win11 SSD实机和真实ERP联调仍开放，`blocked_security_review` 状态不变。

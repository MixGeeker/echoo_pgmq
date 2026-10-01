# 未采用的SQL机制原型

仅用于可丢弃测试数据库。两个函数是原claim的等价命名副本与zero-count实验，均REVOKE PUBLIC；不会自动替换产品claim，也不属于安装脚本。不要在生产数据库加载。

基于470767e的SQL0.1.1。B保留auth、参数检查、v_now与只读事务25006回落，仅在读写事务新查到message_count=0时早退。它跳过原空UPDATE的statement triggers，触发器重入还可能观察中间计数；不宣称完全等价。

完整功能/微实验/覆盖率与未采纳决定见 ../../../docs/performance-empty-claim.md。若未来决定产品化，仍需版本化SQL迁移、普通并发/升级/备份测试与原生端到端验证，不能直接把本原型当发行包。

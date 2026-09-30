# 开发与验收进度

最后核实：2026-09-30 08:00 UTC。草稿PR#1继续开发，尚未达到生产发布标准。

## 当前代码与主目标

PostgreSQL **18** 为主支持/验收版本，Linux和原生Windows都必须通过；PG16/17为补充。部署为PG原生扩展及受管理后台worker直接提供AMQP1.0，不需要外部broker、sidecar或Docker。

当前被测代码为**37fc015**：已撤回472e626的提交后唤醒实验，src/include/SQL/CMake/control共9个远端blob与此前96c18db/a501852完全相同。撤回因尚无稳定收益证据，不能解释为已经证明代码导致退化。两次固定预算实验、已舍弃方案和不利结果全部保留。

## 当前已验证的普通兼容

[CI36679556956](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36679556956)与[完整验证记录](validation.md)：

- Linux/原生Windows Server2022 × PG18/17/16六组合，源码安装和候选归档重装各一轮
- PG18每轮40项（含独立JavaScript rhea8项），PG16/17每轮32项；合计416项，0失败/错误/跳过
- 12个core.sql完成标记、12份JUnit/environment、12外层artifact、6内层候选ZIP及全部manifest文件散列逐一核验
- 实际checkout/manifest为GitHub测试合并537d2f0，与PR头37fc015同tree2ba52d8；没有合并到main
- 归档重装后移除构建Proton/OpenSSL库搜索路径再测。Node24.21.0/rhea3.0.5覆盖指定正常地址、binary/metadata、FIRST/SECOND确认、release及正常关闭后新连接重投；不代表任意AMQP客户端

普通回归已验证实际扩展安装、SQL业务事务入队/回滚、并发领取/幂等/配额、身份与ACL、收据代次、正常发布/消费/重投、0.1.0→0.1.1及失败SQL事务回滚、逻辑备份恢复正文/ACL/租约/序列。它不包含暂停中的完整安全/故障资格。

## 性能实测与撤回

| 固定提交/运行 | 实际证据 | 必须保留的限制 |
|---|---|---|
| 51ca717 / 36663717022 | 27单元，每单元30秒暖机+120秒测量；含各阶段1,077,932条全部对账/排空，0丢失/重复 | echoo12/12、Rabbit10/12未达暂定合成DB p95增幅≤10%；echoo尾延迟与资源结果未显示优势 |
| 472e626 / 36672765081 | 27单元与1,000,146条全部对账/排空，0丢失/重复；完整ZIP与728文件散列及两份原始复算通过 | echoo11/12低于95%目标到达率，24/24代理单元未达DB目标；PG-only自身也只达约165–173TPS/目标200，不能作严格A/B |

两次整个服务端栈均为2逻辑CPU/4GiB/0swap，客户端另2CPU，同AMQP1/mTLS/持久化/手动ACK；共享runner不同且image变化。闭环逐条确认在变慢时会自行少发，不能把已完成速率说成开放到达的容量上限，或把较少工作量下的CPU降低说成效率提高。

详见[首次固定预算报告](benchmark-results.md)、[唤醒试验完整报告](benchmark-wake-results.md)、[本地机制试验](worker-performance.md)。新smoke步骤通过，但其单独附件因传输失败未完成独立原始核验；完整27单元附件的验证已完成。

## 短诊断已停止：环境门禁失败，本轮性能研究收口

[只读诊断与短实验提案](performance-diagnosis.md)已核对新旧2,078,078条ID/时钟来源：无sent_ns差异、负时长或窗口归属异常。新长confirm中93.05%与合成DB长事务重叠，优先区分PG/环境共享停顿、同步claim与Accepted出socket顺序；TCP batching仍是竞争解释，40ms阶梯未得到直方图支持。

唯一一次[有界短诊断](ordinary-tail-results.md)已在隔离提交8b333f1执行：前置PG-only的20秒窗口完成137.15TPS，低于预设190TPS门槛（目标200），p99为96.808ms。G0失败后32.69秒即正常结束，六个A/B单元和后续三个G全部未跑；没有重试或第三轮长矩阵。完整33文件原始附件及29项manifest已核验并保全。工作流绿色仅代表成功保存environment_insufficient终态，不是性能通过。

本轮性能研究到此收口。后续比较需要独立、稳定的测试环境先通过同一PG-only门槛，再单独规划；不为通过而改阈值，不归因wake，不恢复实验代码。产品与普通兼容证据仍保持37fc015运行时代码。

## 已发现并修复的产品问题

1. 受拒绝link重复释放曾导致原生崩溃，已修复；历史重复拒绝回归记录保留
2. 原始SQL空/过大/非法消息曾永久阻塞队首，已改为有界扫描并保留死信；不重编码合法消息
3. settle成功标志曾可能早于commit成功，已移至commit之后；暂停前PG17单项注入通过，不代替最终故障资格
4. poison隔离与配额锁序、FK KEY SHARE/queue计数锁冲突已修复，普通并发回归通过
5. 逻辑恢复singleton/序列与扩展默认版本陷阱已修复；恢复需显式安装源版本
6. Windows Proton/MSVC、私有解压目录ACL、SChannel空密码P12、ZIP名称及PG16 psql漏执行core.sql等兼容/测试问题已修复并实证重测

## 未通过或未运行的发布闸门

- 人工安全审查不完整，存在未验证的解析资源上限静态候选；相关调查、新fuzz与故障注入暂停。security/qualification保留手动但不触发，全部候选manifest仍为blocked_security_review
- 历史d24d57a的自动GCC、ASAN/UBSAN及Python依赖检查通过，只代表那些具体检查；普通CI或历史进程恢复测试不能替代完整安全资格
- 实际Win11 x64四核/8GB/SSD、服务身份/门店环境、物理断电、磁盘故障、运行中DLL占用及完整升级失败恢复、长期checkpoint/vacuum/积压尚未最终验收。进程强杀不等于物理断电
- 真实ERP联调按约定后续共同进行；实际业务峰值未知，2倍峰值容量和ERP p95≤10%仍是待校准、未通过的目标
- 项目许可证待所有者决定；候选安装器只适用于显式可丢弃测试安装，未做正式签名/生产发布

首版仍为至少一次；不支持AMQP0-9-1、复杂exchange/广播、broker事务、独立broker集群或端到端exactly-once。普通logged表和同步提交不改变；全局容量锁持有到外层SQL业务事务结束的争用代价仍需代表性评估。当前不合并、不发布正式版本、不部署生产。

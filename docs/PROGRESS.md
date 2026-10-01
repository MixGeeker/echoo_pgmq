# 开发与验收进度

## 标准Apache-2.0与分发通知（2026-10-01）

所有者已选择标准Apache-2.0、署名MixGeeker；本次独立许可变更提供原文LICENSE、项目NOTICE、可选关于页致谢，以及候选打包携带与散列回归。第三方归属不改，已发.1/.2包与guide冻结；历史许可待定记录不回写。变更仅在独立PR中待审，不代表正式发布或完整第三方/安全审查通过。新增普通打包用例后矩阵预期476次，实际结果以本PR精确提交的CI为准。

下方各节保留历史时间点的决定与测试来源；main整合已在a2ae350完成，合前/合后普通CI分别为36801443412与36802047524，各464次，见已合并PR1。

最后核实：2026-10-01 01:28 UTC。PR4已合入PR2工作分支（91044752），PR2已合入PR1工作分支（1954e4f2），两次整合tree均为19fbab25e4dac3bcffae91c6403c6a9691a54ac6。PR3的SQL0.1.2仍独立保留。此次代码整合已获确认，最终普通CI核验后才能合入main；不宣布生产就绪，也不修改许可证或blocked_security_review。

## 预发布整合的验证要求（2026-10-01）

本次仅整理文档与已验证分支；运行时、SQL、测试和构建脚本与固定450基线一致，保留.2包装器配置修复及已发ZIP/guide字节。最终文档提交将触发自己的普通矩阵与适用的预览包装器检查，执行状态和确切head以[PR1检查记录](https://github.com/MixGeeker/echoo_pgmq/pull/1/checks)为准。在这些检查完成前，不能拿下方历史464次通过当作新head通过。

合并main与生产资格分别处理：完整安全/故障资格仍暂停，Win11/真实SSD/ERP/长稳/签名与许可仍有未完成项。此后2026-09-30各节保留当时状态与负结果，不是本次整合的最终CI或发布声明。

## 附带普通CI六组合最终通过（21:16 UTC）

[CI36776023129](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36776023129)与[独立核验](evidence/preview2-ordinary-36776023129.json)：Linux/Windows Server2022×PG18/17/16全部成功，源码及归档两轮共464次普通执行，0失败/错误/跳过，12个core完成标记。12外层artifact、6内层候选全部manifest/hash及12份JUnit/environment、6份job日志均复核。Linux16/18曾停留软件包安装，随后正常完成；前两个旧run按既有concurrency被新提交取消，不拼接为全通过。

测试head fd30a1f、实际checkout ad589825/tree8d8832，后续提交只更新报告。此重建不替换.2已经验证并交付的原450二进制，也不扩大普通清单为安全/故障资格。当前没有等待判定的CI；保持隔离预览状态、不合并main。

## preview-20260930.2配置修复已完成双平台普通闭环（21:02 UTC）

[.2入门、逐项验证与失败历史](https://github.com/MixGeeker/echoo_pgmq/blob/bce8ff98d7a6bc7d6820d8646a03a304c28c029e/docs/preview-20260930.2/README.md)。保留450681d原内层ZIP/extension0.1.0，仅新ROOT包装器把SQL全局、生成队列与worker上限统一为65,536最终编码字节；不是64KiB业务payload，也不是0.1.2迁移。SQL先包装再按octet_length计量，worker检查同一存储bytea长度。旧.1及其历史字节不替换，.2拒绝将旧ROOT当成已升级实例。

Linux PG18.6七阶段通过；Windows Server2022 PG18.6 [36776023123](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36776023123)实跑七阶段和12项离线回归通过。原ZIP、12guide字节、控制器/输入hash、SQL事务demo、三个既有rhea场景、新增12字节SQL→AMQP远端SECOND、worker/global/5队列实际限额与归零、正常stop/status3均核验。独立只读窄正确性复核未发现阻断，不等于完整安全审阅。

最初Linux控制器错计12字节为11及TIME_WAIT探针错误、Windows LF→CRLF散列失败及固定端口WinError10013均保留。最终仅新guide固定eol=lf，CI使用OS分配可绑定loopback端口；未改用户默认端口、系统权限/防火墙或放宽散列。Windows测试head fd30a1f，实际checkout ad589825整树多18项报告/未安装诊断patch，运行时/guide无差异。六组合普通重建CI36776023129仍单独收齐终态，不以它替代现有原450的464次证据。

两平台.2独立套件已完成17成员及SHA256SUMS核验，Windows536119字节（ae2feb17…），Linux5730143字节（f3f64706…）；完整hash在上述验证页。每包附最终VERIFICATION覆盖guide中生成时的准备快照。仍只供可丢弃本机合成数据测试；blocked_security_review及Win11/真实SSD/ERP/生产SLA门槛未满足，不合并main或正式发布。

## 本轮优化收敛与预览契约修复（20:38 UTC）

[阶段总结](performance-round-conclusion.md)集中列出唯一重复CPU正向候选七计划、未采用路径及剩余高价值方向。本轮不再追加没有新证据的微优化负载；桥计时与等待机会不等于可省CPU或已实现加速。已发布预览使用七计划，产品主工作分支及main未据此升级，安全/故障等门禁仍未满足。

[preview-20260930.1外部契约v1](preview-20260930.1-contract.md)核对了7个PUBLIC SQL函数、实际配置、AMQP子集、已测/代码支持/未验证的区别。发现普通接入配置错位：AMQP检查完整编码65,536字节，SQL默认允许1MiB；合法SQL消息可能入队成功后在AMQP领取转dead。已确认两入口计量同一最终encoded bytea，正在准备.2包装器统一限额及合法小消息普通验证，尚未宣布修复通过；不改.1已交付字节或原二进制。TTL/expiry/priority/group等只透传，没有对应broker语义或逐消息提示。

## 新增：正常跨队列等待与恢复，实用建议优先（20:15 UTC）

[完整报告与逐次时延](performance-global-contention.md)：固定预览450原包/SQL0.1.0，Linux PG18.6、独立rhea。两类正常场景各2次基线和3次争用观察，10次全部通过，实际5.328秒。无争用发布2.27/4.88ms、远端消费结算1.25/1.47ms；先证明worker正在等待本轮真实SQL后台PID，再保持150ms并正常COMMIT，发布和远端第二结算均约157–158ms。释放后正文/receipt断言、计数和排空通过，没有锁超时、拒绝或故障注入。

独立复核375事件、6个精确锁等待、31份计数快照、20个正常Node退出、前后映射库身份和同执行会话PG清理。只有少量正常观察，不新增CI矩阵计数、不计算p95/容量，不代替Windows/实机ERP/SSD资格。证据中未保存完整stored行/receipt，离线复算不伪称重新校验这些未保存的数据。

实用方向是业务允许时将enqueue放在事务最后、紧接COMMIT，并统一业务行→队列容量的锁顺序，减少持锁期间的客户端往返与外部等待；保持业务和消息同事务，容量不足仍整笔回滚。具体业务调用顺序尚未真实ERP联测。增加worker不会消除同一全局限额行的争用；预分配额度等架构方案会带来利用率/迁移/恢复取舍，本轮不静默改变严格总容量合同。

[deadline历史复核](performance-deadline-reassessment.md)结论为不重开。新trace的已发布未领取消息相关超期等待2.498秒/89.99秒、名义可缩短timeout包络1.431秒都不是收益；此前四arm30单元已见独立deadline臂无稳定收益且空claim/消息均增加。旧变体只找到了可信源码hash，不能断言fresh-empty条件完全相同；这一未知不足以压过旧负结果。没有为此修改产品或跑新负载。

## 新增：事务桥分阶段诊断，G2漂移后按预案停止（19:38 UTC）

[完整诊断报告](performance-bridge-phases.md)使用同参数CMake构建的原始450七计划A与仅加累计计时的B，两臂SQL0.1.1；计时代码只作为隔离patch保留，未进入产品运行时。242.76秒后正常停止：各PG-only吞吐门禁通过，但G1→G2的p99由1.125367升至2.453986ms，绝对漂移1.328619ms超过固定1ms，所以P1有效、P2失效、P3未跑，无重试。

有效P1-B的完整worker生命周期中，query+data占已计时桥操作elapsed的86.68%，begin占5.71%，commit占7.61%。三类有消息操作各6,996次，与全部生命周期ID闭合，不能用30秒5,996条cohort当分母；不是CPU份额或可直接省去的成本。下一步应定位查询/数据路径与代表性业务事务争用，暂不重复SET合并或WaitEventSet微优化。

P1计时扰动对照中B的确认/收件E2E p99低0.236/0.254ms、worker CPU/实际收件低3.72%，但DB均值高0.0329ms、E2E/DB最大值也高；一个有效对不能证明插桩零开销，更不是新的产品优化收益。完整166原始文件、27,981条全阶段ID、源码/构建/实际映射及正常清理均核验；同执行会话81个PG身份均退出。P2全部不利/有利值保留并标无效，云端易失存储与CPU亲和性边界不变。

既有长SQL事务跨队列测试源码不在当前ordinary清单中，464项不能代表该场景已验证；此处19:38时仅在准备短场景；20:15新增章节已记录独立执行结论，未把它加计为464项CI。安全/故障资格继续暂停，不合并或升级已发预览。

## 新增：固定预览空闲资源实测（19:00 UTC）

[完整中文报告](performance-preview-idle.md)基于已发预览的原始 Linux 归档和 SQL 0.1.0，未重编译或改产品。六阶段 PG-only前基线→无连接listener→1/4/8个正常空闲连接→PG-only后基线，133.61秒完成；前后稳定性门禁通过，60个原始文件和各阶段正常清理均复核。

0/1/4/8 个空闲连接时 worker CPU 为单核的 0.138%/0.483%/0.966%/1.242%，但最小值只有2个10ms计时tick，不能作精细优化收益。8连接全PG采样CPU为单核5.453%，较前后基线增加1.248/1.402个百分点（相对+29.7%/+34.6%）；PSS为56.45MiB，增加7.79/9.00MiB。各内部阶段合成DB保持200TPS，p95为0.698ms（前后0.725/0.788ms），p99为1.608ms（前后1.386/1.550ms）。不利结果与双方基线同时保留。

这是一轮短时、顺序、空闲连接成本诊断，未发送MQ消息，不建立容量、长期内存、因果或真实ERP验收结论。暂定10%目标针对ERP/DB p95，不能套到p99或用本轮推翻既有未达结果。首轮控制器未关闭Node标准输入导致未完成清理；失败记录、定位控制管道的正常关闭探针及修正后的完整第二轮分别保留。产品二进制和门槛均未改，安全/故障资格继续暂停。

## 固定 PG18 临时测试包已交付，并补齐 Windows 入门脚本实跑

预览标识为 **preview-20260930.1**，扩展及内层候选包版本仍为 **0.1.0**（附0.1.1升级脚本），固定源码 **450681d71c2fefef0a92e57098643701dabcf4f5**，来自下述464项普通矩阵。临时测试选用七计划候选，不等于已合并main或已证明整体性能提升。已发Windows/Linux包的字节保持不变；后续实验SQL0.1.2未装入该预览。

[中文入门与追加验证](https://github.com/MixGeeker/echoo_pgmq/blob/4b28f1c3e92830c1471bf1038bd375dad2393899/docs/preview-20260930.1/README.md)包括私有新PG前缀、新数据目录、loopback、临时mTLS配置、SQL及独立rhea示例、正常停止和清理边界。Linux PG18.6已实际完成；Windows Server2022 PG18.6的原始归档和12个原始入门文件经散列验证，[36754291541](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36754291541)与[36754039681](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36754039681)均完成6个阶段、3个rhea正常用例及正常停止。此前OpenSSL预检查失败和PIPE捕获超时的两次尝试也已归档，不计作通过。

用户收到的是可丢弃隔离环境测试包，不用于生产库；GitHub Release尚未创建。Windows Server不是Win11实机。新验证只修CI控制器，没有替换用户已收到的.1包。

## SQL 0.1.2 容量预留候选：普通正确性通过，原生性能未支持采用

隔离[PR#3](https://github.com/MixGeeker/echoo_pgmq/pull/3)及[完整报告](https://github.com/MixGeeker/echoo_pgmq/blob/7eb7409a75a9b80a4d1460cd099c434c37bdddc1/docs/performance-sql-reservation.md)保留普通SQL内部预留候选。[CI36749616970](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36749616970)测试head67df046，Linux/Windows Server2022×PG18/17/16源码及归档重装12轮704项、0失败/错误/跳过；实际SQL身份、12个core结束标记和归档散列均核验。Windows16/17最初CRLF身份失败已修夹具并保留历史。

单个外层SQL事务内批次backend CPU的局部下降不能当AMQP收益。原生3对的worker CPU/实际收件分别+8.62%/+3.65%/−0.76%，确认/投递尾延迟混合，DB p99三对均上升；41,975条全阶段ID完整对账、4gate/3drift通过，坏结果不删。本轮不把0.1.2推广进固定预览，不改对外契约或历史评分。

## 新增：早发送候选，漂移门禁后保留实验（15:03 UTC）

[完整早发送报告](performance-early-flush.md)：基于七计划，仅提早已有响应的非阻塞发送，Linux PG18基线/候选各45项普通回归与core.sql通过，未跑该候选的Windows/新CI。245.98秒后G2因PG-only p99由3.176降至1.513ms、绝对变化超过对称稳定门限而正常停止；P1有效、P2失效、P3未跑，不重试或改阈值。

唯一有效对确认p99降低14.70%，但投递/DB p99上升5.31%/4.23%，worker CPU/实际收件仅降1.46%，不足以建立整体净收益；暂不接入产品或PR2运行时，只保留实验patch。27,956条全阶段消息对账、164项原始证据和正常清理核验；P1-B测量终点1条未收件随后排空，短暂后台PID的未完整CPU计量明确披露，不能称总CPU优势。继续既有trace的普通机制成本分析，完整安全/故障资格继续暂停。

## 新增：zero-count局部收益与覆盖率，停止本轮接入（14:25 UTC）

[完整SQL微实验及native覆盖率](performance-empty-claim.md)：真空队列backend批次CPU/调用降低28%–47%，未来租约仍保留时增加3.5%–26%；不是AMQP产品收益。21项来源/普通行为检查、80原始项和4gate/3drift核验通过，128.5秒正常完成。夹具创建调用过C编码器，已更正原始元数据中过宽的“二进制未使用”表述，未改写历史。

旧native trace重建显示，七计划B仅31.65%的全部claim真正retained=0，约52%的“空claim”仍有未ACK消息。覆盖率与额外点查成本不能直接推出净收益，却需SQL迁移/trigger兼容约束，故本轮不接入产品、不再追加该候选矩阵；原型与坏结果完整保留。下一方向是有界响应发送顺序，先普通审查/回归，尚无收益结论。当前产品与七计划运行时均未因这些实验改变。

## 新增：三SET合并实验未采用（13:15 UTC）

[完整负结果与来源](performance-batched-settings.md)：在七计划A之上把3个SET合为1次SPI调用，三对自有PG CPU分别+15.89%/+6.91%/+2.63%，确认/投递/合成DB的均值及p50/p95/p99也全升。本轮不支持推进该B，未改PR2运行时；这不否定此前七计划的CPU信号，也不是凭单个max否决。41,890条全阶段ID完整一致、4gate/3drift有效、353.36秒正常结束，全部原始与有利指标保留。

保留新增的正常queue行锁超时55P03→同sender/worker恢复测试，本地A/B各44项+core.sql通过。七计划加此测试的[CI36720731289](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36720731289)已核验：Linux/Windows Server2022×PG18/17/16、12轮共464项，0失败/错误/跳过，12个core.sql标记和12次本轮55P03日志、全部内外归档散列一致。被测head450681d，实际checkout191d994与其同tree d0abc20；PG18每轮44项，PG16/17每轮36项。此前452项仅覆盖此前集合；本次更新纯报告不再重跑矩阵。当前产品采用决定仍为基线，七计划保持独立审查候选，继续SQL路径普通分析。没有恢复暂停的安全/故障资格。

## 新增：云端同机机制与普通兼容（12:40 UTC）

[完整中文报告](performance-local-results.md)和[客户端延迟与服务CPU配对汇总](performance-local-summary.csv)已记录四轮实验及坏结果。七计划候选470767e无native插桩确认，三对自有服务CPU下降9.29%/6.59%/12.77%，但第2对确认/投递/合成DB p99上升16.81%/6.76%/31.17%，前两对最大延迟也变差。产品采用决定保留22a5ccc运行时基线；PR2保留候选，不合并。

七计划[普通CI36711924070](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36711924070)的Linux/Windows Server2022×PG18/17/16，12轮共452项，0失败/错误/跳过；12个core.sql标记和全部内外归档散列已核验。它不替代暂停中的完整安全/故障资格。

四轮同机PG-only门禁都通过，最后一轮41,858条全阶段消息精确对账、正常清理。环境仍为overlay fsync=volatile/tmpfs、只有CPU亲和性；这是机制证据，没有持久SSD/Win11/ERP/容量结论。12:40文档提交不改运行时，CI绑定报告列出的固定代码提交。

## 当前代码与主目标

PostgreSQL **18** 为主支持/验收版本，Linux和原生Windows都必须通过；PG16/17为补充。部署为PG原生扩展及受管理后台worker直接提供AMQP1.0，不需要外部broker、sidecar或Docker。

历史普通兼容基线为**37fc015**；本轮A为**22a5ccc**，二者运行时源码相同、完整Git tree不同：已撤回472e626的提交后唤醒实验，src/include/SQL/CMake/control共9个远端blob与此前96c18db/a501852完全相同。撤回因尚无稳定收益证据，不能解释为已经证明代码导致退化。两次固定预算实验、已舍弃方案和不利结果全部保留。

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

## 历史 GitHub 短诊断：环境门禁失败，那个实验已结束

[只读诊断与短实验提案](performance-diagnosis.md)已核对新旧2,078,078条ID/时钟来源：无sent_ns差异、负时长或窗口归属异常。新长confirm中93.05%与合成DB长事务重叠，优先区分PG/环境共享停顿、同步claim与Accepted出socket顺序；TCP batching仍是竞争解释，40ms阶梯未得到直方图支持。

唯一一次[有界短诊断](ordinary-tail-results.md)已在隔离提交8b333f1执行：前置PG-only的20秒窗口完成137.15TPS，低于预设190TPS门槛（目标200），p99为96.808ms。G0失败后32.69秒即正常结束，六个A/B单元和后续三个G全部未跑；没有重试或第三轮长矩阵。完整33文件原始附件及29项manifest已核验并保全。工作流绿色仅代表成功保存environment_insufficient终态，不是性能通过。

该GitHub实验已结束。其后按用户要求转到新的云workspace，进行了上方独立同机实验，先通过相同PG-only门槛；没有重跑失败runner刷绿或覆盖其不利证据。产品工作分支仍保持37fc015对应运行时代码，隔离PR2另有明确的候选与证据。

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
- 项目许可证现已选定Apache-2.0；完整第三方分发审查仍待完成。候选安装器只适用于显式可丢弃测试安装，未做正式签名/生产发布

首版仍为至少一次；不支持AMQP0-9-1、复杂exchange/广播、broker事务、独立broker集群或端到端exactly-once。普通logged表和同步提交不改变；全局容量锁持有到外层SQL业务事务结束的争用代价仍需代表性评估。本次授权允许普通CI核验后的main代码整合；正式生产发行与部署仍未放行。

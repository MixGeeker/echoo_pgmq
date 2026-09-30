# 事务桥分阶段诊断：主要成本在查询与数据处理

2026-09-30。这里只追加诊断报告与隔离patch，产品运行时和已发预览没有变化。[完整机器结果](evidence/bridge-phase-20260930.json)、[全部配对效应CSV](performance-bridge-phase-pairs.csv)、[累计阶段CSV](performance-bridge-phases.csv)、[诊断patch](../bench/experiments/bridge-phase-diagnostic.patch)可与报告交叉检查。

完整原始证据包为 `bridge-phase-evidence.zip`，3,883,100字节、250项，SHA256 `4098ac755293e69ed46c4fa7416a434d5d1b5fa64c6f15f8c5742ba565a945c8`。含166份raw及原清单；解压后可执行 `python recompute_bundle.py --bundle-root . --output ../portable-result`，只用Python标准库。便携复算275,028项通过，不包含二进制，也不重新观测已经退出的进程；完整原workspace审计另有275,592项。这些是数据一致性检查数，不是产品测试数。

结论：优先检查 work 内的查询和数据处理。有效 P1-B 中，它占已计时 bridge 阶段累计 elapsed 的 86.68%；begin 5.71%、commit 7.61%。这里的 work 含实际 SQL、授权检查、数据拷贝和结果处理，并不等于可删除的开销。begin+commit 的13.32%只是已测范围中的理论包络，不能承诺省掉这些时间或按此预测 E2E 加速。

这轮用于衡量诊断插桩扰动：A 是固定450681d的七计划基线，B仅增加累计阶段诊断。P1有效，P2因G2漂移失效，P3未执行。一个有效短对不能建立插桩成本的因果值、最坏上界、统计显著性或“开销可忽略”结论。

## 门禁和停止

G0/G1/G2 的 DB p99 分别为1.658221/1.125367/2.453986ms；三门禁的全窗和两个半窗开始/完成吞吐均≥190TPS。G0→G1绝对变化0.532854ms≤1ms，P1有效；G1→G2变化1.328619ms>1ms，P2失效。固定规则为相邻完成TPS变化≤5%，p99绝对变化≤max(1ms，前门禁p99×50%)，没有修改门槛或重试。

同executor wrapper耗时242.755049s，controller耗时242.642867s，正常exit=0。7份cleanup均正常停止，PG日志最后均为shutdown。81个原始PID+创建时间身份与wrapper计数闭合，wrapper记录全部已退出、无剩余子进程、无恢复回退、私有状态已删除。审计不把另一个命名空间中的当前PID检查当成原运行清理证据。

## P1有效对：全部主要分布

顺序BA；单位ms。Δ=B−A，百分比=B/A−1。确认与E2E按发送起点落在30秒窗的cohort；DB按事务起点入窗，保留晚完成。吞吐分别按实际发送/确认/收件时间入窗。分位数为sorted[floor((n−1)p)]。

| 分布 | mean | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| 发布确认 | 0.909180 → 0.894391；Δ -0.014789 (-1.63%) | 0.844283 → 0.854108；Δ +0.009825 (+1.16%) | 1.322935 → 1.256645；Δ -0.066290 (-5.01%) | 2.069190 → 1.832944；Δ -0.236246 (-11.42%) | 13.998877 → 8.330832；Δ -5.668045 (-40.49%) |
| 收件E2E | 1.315083 → 1.158917；Δ -0.156165 (-11.87%) | 1.042081 → 0.969902；Δ -0.072179 (-6.93%) | 3.120846 → 3.069743；Δ -0.051103 (-1.64%) | 3.946239 → 3.692202；Δ -0.254037 (-6.44%) | 14.445901 → 15.081395；Δ +0.635494 (+4.40%) |
| 合成DB | 0.558430 → 0.591328；Δ +0.032897 (+5.89%) | 0.460014 → 0.524421；Δ +0.064407 (+14.00%) | 1.018641 → 0.921550；Δ -0.097091 (-9.53%) | 1.803930 → 1.769578；Δ -0.034352 (-1.90%) | 13.535065 → 14.095348；Δ +0.560283 (+4.14%) |

P1确认/E2E的p99下降，但确认p50、DB均值/p50和E2E/DB最大值上升，均完整保留。B的E2E最大值增加0.635494ms（+4.40%），DB均值增加0.032897ms（+5.89%）。这些双向差值是本次插桩对照的观测范围，不能全部归因于四次读钟，也不能由CPU下降反推插桩产生负开销。

## CPU和负载核对

| Cell | 全生命周期ID | 测量cohort | 实际采样跨度s | 同跨度收件 | 持久PG CPU s | worker CPU s |
|---|---:|---:|---:|---:|---:|---:|
| P1-B | 6996 | 5996 | 29.644972 | 5926 | 6.95 | 5.65 |
| P1-A | 6998 | 5998 | 29.654593 | 5928 | 7.11 | 5.87 |
| P2-B | 6997 | 5999 | 29.645418 | 5928 | 7.34 | 6.01 |
| P2-A | 6990 | 5990 | 29.653818 | 5920 | 7.34 | 6.02 |

P1 worker CPU ms/实际收件：0.990216 → 0.953426；Δ -0.036790 (-3.72%)；12个持久owned身份子集：1.199393 → 1.172798；Δ -0.026595 (-2.22%)。

四个cell各60个测量资源样本，均为同12个PID+创建时间，worker映射身份贯穿全窗；本轮测量样本未观察到transient。持久子集含PG后台进程、合成DB及观察连接等，并非纯消息处理CPU。采样间完全出现又退出的进程、未观测CPU尾部仍可能漏掉，不能设为零或给它上界；不是总服务端/cgroup CPU。归一化只用约29.65秒采样跨度内的实际收件数，不外推30秒。RSS和可能重复计入共享页。

四cell全生命周期共27,981个ID。producer/consumer ID唯一、run/连续序号/发送时间戳完全闭合，accepted与valid全部为真；无重复、缺失、意外消息或校验失败。payload实际字节未保留，不能独立重算接收字节SHA，只能核验固定校验代码与逐条通过标记。P2-A末窗1条晚确认、晚收件保留在cohort；所有before/after队列深度均0。

## B累计阶段：从原始PG日志重新解析

以下计数和累计时间覆盖整个worker生命周期，包括warmup、drain和空轮询。不是30秒客户端窗；没有逐事件native trace、独立时间戳轨迹或阶段分位数。各阶段是四个instr_time边界形成的聚合elapsed，含等待/调度，不是CPU。

| Cell/资格 | 类别 | 完成次数 | begin均值µs | work均值µs | commit均值µs | begin/work/commit占本类别% |
|---|---|---:|---:|---:|---:|---|
| P1-B/有效P1 | publish | 6996 | 9.612 | 193.570 | 17.533 | 4.36 / 87.70 / 7.94 |
| P1-B/有效P1 | claim_empty | 14493 | 8.869 | 80.140 | 5.503 | 9.38 / 84.79 / 5.82 |
| P1-B/有效P1 | claim_success | 6996 | 6.121 | 143.074 | 12.915 | 3.78 / 88.26 / 7.97 |
| P1-B/有效P1 | settle | 6996 | 8.435 | 143.221 | 14.861 | 5.07 / 86.01 / 8.92 |
| P2-B/无效P2，仅留存 | publish | 6997 | 9.950 | 232.543 | 18.820 | 3.81 / 88.99 / 7.20 |
| P2-B/无效P2，仅留存 | claim_empty | 14244 | 9.379 | 83.686 | 5.613 | 9.50 / 84.81 / 5.69 |
| P2-B/无效P2，仅留存 | claim_success | 6997 | 6.463 | 143.482 | 13.712 | 3.95 / 87.67 / 8.38 |
| P2-B/无效P2，仅留存 | settle | 6997 | 8.196 | 151.499 | 16.288 | 4.66 / 86.09 / 9.26 |

P1-B publish/claim_success/settle各6,996次，与全生命周期6,996个accepted/received ID闭合，不能拿5,996条测量cohort作分母。空claim14,493次，即每成功claim约2.072次；其累计elapsed占全部阶段26.28%，可以作为轮询/query调查的线索，但不能据此直接删轮询或取消事务。P2-B对应6,997/6,997/6,997与空claim14,244次，也闭合；仅保留失效对描述。

P1-B累计begin/work/commit为0.297629/4.518602/0.396732秒，合计5.212962秒；按全生命周期收件摊销，begin+commit约99.251µs/条（包括空claim摊销）。只看publish+成功claim+settle的三项均值相加，begin为24.169µs、work479.865µs、commit45.309µs，begin+commit共69.477µs。以上均不是单条消息路径延迟或CPU，不能与30秒CPU相减或直接作为E2E节省。P2-B合计5.610438秒，work87.04%、begin+commit12.96%，不得恢复其效应资格。

begin包括事务/snapshot/SPI设置、三条已缓存SET LOCAL的执行和timeout启用；不能独立归因于SET。work包括授权函数、查询执行、行与payload处理：publish含quota锁/插入/计数，claim含租约和所有权变更，settle含receipt验证和状态变更。commit包含timeout关闭、SPI_finish、snapshot清理和CommitTransactionCommand/等待；不是单独WAL flush。下一步应先在不弱化语义的前提下检查work中的具体SQL/数据路径，再评估begin/commit可摊薄部分。

claim_empty只表示返回无可用消息：一般情形下SQL仍可将过期且重试耗尽的消息dead-letter，bridge也可拒绝poison消息后返回empty。不能把它普遍当只读、无副作用。此次正常负载summary的caught errors、settle not_applied、authorization errors均0；publish/claim/settle的started=completed+errors闭合。失败调用的耗时与abort不计入成功阶段累计，正常负载无该项遗漏。授权excluded=2仅指单独link授权调用，work内SQL授权检查仍存在。

## P2失效原始数据：不得并入有效结论

两cell负载达到单元条件，但后置G2漂移失败。坏值和好值均保留；P3无结果。

| 分布 | mean | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| 发布确认 | 0.949806 → 0.952801；Δ +0.002994 (+0.32%) | 0.862861 → 0.888750；Δ +0.025889 (+3.00%) | 1.372039 → 1.294432；Δ -0.077607 (-5.66%) | 2.672423 → 2.201600；Δ -0.470823 (-17.62%) | 27.575988 → 7.681016；Δ -19.894972 (-72.15%) |
| 收件E2E | 1.260267 → 1.321938；Δ +0.061671 (+4.89%) | 1.000593 → 0.996192；Δ -0.004401 (-0.44%) | 3.169158 → 3.280546；Δ +0.111388 (+3.51%) | 4.264635 → 3.839965；Δ -0.424670 (-9.96%) | 27.664131 → 8.882102；Δ -18.782029 (-67.89%) |
| 合成DB | 0.554839 → 0.537916；Δ -0.016923 (-3.05%) | 0.480915 → 0.495712；Δ +0.014797 (+3.08%) | 0.870182 → 0.784873；Δ -0.085309 (-9.80%) | 1.931672 → 1.445059；Δ -0.486613 (-25.19%) | 16.262660 → 7.852424；Δ -8.410236 (-51.72%) |

P2 worker CPU ms/收件：1.016892 → 1.013833；Δ -0.003059 (-0.30%)；持久owned子集：1.239865 → 1.238192；Δ -0.001673 (-0.13%)。所有额外CPU/RSS/调度指标的原值、绝对和相对差值见[全部配对CSV](performance-bridge-phase-pairs.csv)及机器JSON。

## 身份、复现与边界

- 原始MANIFEST完整覆盖166份结果，逐项SHA重算无差异；127个诊断基线源文件与固定Git blob、6个新增诊断文件、19个A/B构建源码条目及adapter/toolchain固定哈希核对。patch在内存逐hunk重建B成功，未写源目录。275,592项明确检查通过，检查数不是性能样本数
- A commit：450681d71c2fefef0a92e57098643701dabcf4f5；SQL 0.1.1在四cell前后快照均确认，A/B三份SQL文件完全相同；固定controller部署A的同一SQL，server明确CREATE EXTENSION VERSION 0.1.1。没有保存每个运行函数的独立catalog定义转储
- A库SHA256：98804dff6e6abafa81a931d69360141d79729ff4e60edccfdaf32b9bf52e7684；B库：13da451342c73b971cf8986b3919de24625d62373c7c2bad6e4f42cc75abe9b0
- 两库匹配CMake RelWithDebInfo/PG18.6/Proton0.40.0、test hooks OFF，规范化compile flags和link命令相同。B固定累计状态符号264B，不包含栈上timestamp、增加代码或既有计划缓存。每cell已保存worker PID/创建时间/映射路径/inode/device/实际SHA前后一致；不是仅检查待加载候选库，也不是重新构建证明
- source/build provenance中的“未运行”是执行前快照；实际加载由本次运行见证补足。lifecycle.json是启动时记录，不用其初始cleanup_confirmed=false推翻后续cleanup/terminal/same-executor最终证据
- 1P1C、1KiB、200msg/s+200TPS、2ms poll、mTLS、durable发布等待remote ACCEPTED、正常manual ACK。E2E截止于receive返回的时间戳，随后校验并发送ACK；不是远端ACK提交延迟
- overlay fsync=volatile及/tmp tmpfs；CPU affinity 0/1和2/3，无cgroup CPU/内存/swap硬上限。开启fsync/full_page_writes/synchronous_commit不让这里的commit成为真实SSD持久化成本。无真实ERP数据
- 仅离线读取固定证据，未启动服务、负载、重试或安全/parser/fuzz/新fault资格流程；这些流程仍暂停。collector对正常日志没有发现计数错误；其truthiness布尔校验可另行加固，本审计独立要求真实bool，未修改collector

复现：python /workspace/shared/bridge-phase-audit/audit_bridge_phases.py。输出independent-raw-analysis.json、pair-effects.csv、bridge-phase-breakdown.csv、cpu-observations.csv、中文结果.md及audit-output-manifest.json。完整原始证据位于/workspace/shared/bridge-phase-mechanism/adapter/result-one。


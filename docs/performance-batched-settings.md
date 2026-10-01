# 合并三条SET的SPI调用：没有显示收益的候选（2026-09-30）

## 决定

本轮不采用“三条SET合为一次SPI调用”的B，继续保留已有七计划A作为PR2审查候选。三对CPU和确认/投递/合成DB的均值、p50、p95、p99共同变差；这不是因为要求每个max都改善才拒绝它。保留P3 DB max明显改善等有利指标，也不将这次结果解释为拒绝全部计划缓存。

没有重跑刷绿。这个普通固定负载结果不证明所有环境下的因果效应，实际机制尚未隔离；产品基线、七计划候选与这个未采用实验分别保留。[此前四轮完整报告](performance-local-results.md)仍有效。

## 为什么选它、实际改变什么

从上一插桩轮同钟native桥接elapsed重算，七计划B里publish占31.28%、空claim23.60%、settle22.83%、成功claim22.28%；约39.1%的桥接调用为空claim、约386次/s、均值96.95µs。**elapsed份额不是CPU份额**，无native插桩轮没有这些内部计数。

所有桥接事务都先执行同样3条SET LOCAL，因此尝试仅省掉2次SPI调用包装与执行上下文切换/reset，不改SQL、轮询调度、确认时机、ACL、租约或事务语义。内部仍保留3个SET语句的CachedPlanSource，按原順序执行synchronous_commit=on、search_path=pg_catalog、lock_timeout=1000ms；不是删掉SET，也不是减少2个snapshot。

A固定470767e60d618a14beb2d69cdedb1e1ac9704ae3；B实验固定本地23304e1aa4606d8a7727279948ea19464e4736db。B不在产品/PR2运行时，完整可复现差异保存在[实验patch](../bench/experiments/grouped-settings.patch)，以470767e为适用基点。该patch是未采用实验，不是推荐安装。两原生输入只有storage.c不同；测试/harness另新增正常锁超时恢复回归。

B storage.c SHA256：ff51ba9d471b6703fdfcf26da52239ed3b1f5836d4cf6a887799ad6774f91685。
A/B实际构建库SHA256：00ebc1cc220146a4190297ebb6899197bf33917081d9272cac13419be4f84ca2 / ff6c7282933ad21d02737919a37ee834212031283da3a2ab6052dbb13c5f4efb。

普通PG18.6源码核查：SPI按顺序执行各工具命令；较早ERROR或负返回不会被后面的成功掩盖。SET仍经过ProcessUtility与GUC，本候选临时分配只在3条后reset，常数峰值可能增加。第三方hook接收的完整源字符串/statement offset、首次parse顺序不同，不能声称任意hook等价。

“SET受search_path改变而反复重规划”这项特定猜测不符合PG18.6核心代码：VariableSetStmt不要求parse-analysis/revalidation，RevalidateCachedQuery直接返回。当前没有证据可把下面的性能结果归因于这个机制，或精确归因其他因素。

## 固定条件与效应

和上一无native插桩轮相同客户端/采样/门禁/控制逻辑，逐字节核对；只更新新A/B源身份、路径与说明。BA/BA/AB三对，4个PG-only gate各5秒暖机+20秒测量，6个cell各5+30秒；353.36秒完成，600秒预算、0重试，全部正常清理。

1P/1C、1024字节、目标200msg/s + 合成DB200TPS，credit32、单条在途、poll2ms，mTLS/EXTERNAL、durable等待Accepted、手动确认。合成DB仍是1000行库存的单行UPDATE、100行范围SUM和提交。各段真实发送198.533–199.900条/s，全部gate及漂移条件通过，不能因此排除cell时的瞬时主机噪声，也不是容量上限。

云workspace overlay fsync=volatile、/tmp tmpfs；PG fsync/FPI/synchronous_commit开启不建立持久SSD证据。仅server CPU affinity0/1、client/observer2/3，无硬cgroup/4GiB封顶。native trace关闭；client计时、ID/checksum、psutil及PG I/O timing仍在。采样CPU可能漏短暂进程，RSS共享页重复计数。

| 对 | 自有PG CPU变化 | worker CPU变化 | 确认p99变化 | 投递p99变化 | 合成DB p99变化 |
|---|---:|---:|---:|---:|---:|
| 1 | +15.89% | +15.24% | +57.81% | +17.08% | +58.06% |
| 2 | +6.91% | +6.00% | +22.07% | +20.41% | +30.29% |
| 3 | +2.63% | +3.61% | +21.84% | +21.22% | +16.79% |

三对变化的中位数（范围）：
- worker CPU/实际收件：+6.24%（+4.11%至+15.45%）
- 自有PG CPU/实际收件：+7.15%（+3.13%至+16.10%）
- 确认p99：+22.07%（+21.84%至+57.81%）
- 投递p99：+20.41%（+17.08%至+21.22%）

三对串行样本不足以建立统计显著性。上表是效应描述和观察范围，没有把几十万逐行核验断言当作独立性能样本。完整均值/中位数/尾值、CPU、内存及调度有利/不利指标见[CSV](performance-batched-settings.csv)。

### 延迟绝对值（A→B，ms）

| 对 | 指标 | p95 | p99 | max |
|---|---|---:|---:|---:|
| 1 | 确认 | 1.341→1.904 | 2.089→3.297 | 8.131→24.546 |
| 1 | 投递 | 3.290→3.741 | 3.897→4.563 | 7.869→25.012 |
| 1 | 合成DB | 1.014→1.813 | 2.125→3.359 | 6.774→28.316 |
| 2 | 确认 | 1.567→1.871 | 2.488→3.037 | 10.265→24.766 |
| 2 | 投递 | 3.446→3.683 | 4.074→4.905 | 11.611→33.929 |
| 2 | 合成DB | 1.444→1.714 | 2.529→3.295 | 9.201→27.443 |
| 3 | 确认 | 1.962→2.138 | 3.437→4.187 | 17.515→56.153 |
| 3 | 投递 | 3.755→4.116 | 5.294→6.418 | 26.871→33.664 |
| 3 | 合成DB | 1.743→1.965 | 3.323→3.881 | 64.809→25.035 |

P3 DB max64.809→25.035ms，改善61.37%，与同对DB p99变差16.79%同时保留；两者不能互相替代。较低RSS也保留在CSV，例如P1/P2 worker均值下降约0.24%/0.28%。确认/投递六个max比较都变差，不过决定不推进B依据CPU及延迟主体分布共同结果，不靠单个极值。

## 数据完整性与普通正确性

六cell共41,890个全阶段消息，35,903条测量发送cohort。逐条ID唯一并全量对齐、accepted/valid标记一致，无观测到missing/duplicate/unexpected；前后队列与测量终点已确认未收件为0。P3-A有1条跨窗完成，cohort保留完整时延、吞吐按真实完成另计。E2E至收到消息后的时间采样，随后才校验；不代表消费者ACK已提交；此次短时正常一致性不是exactly-once或恢复可靠性证明。

233原始文件全集/散列、18原生输入、2库身份、4gate/3drift及正常清理均独立核验。原始全包echoo-batched-settings-evidence.zip为5,709,200字节，301成员，SHA256 **41a6ba7b22817c6e4b5baee99d1ef52f9dba532ae1eb8334b96627ebae4771ab**；已保存交付，非声称全部原始数据已进入Git。复算脚本保留本轮目录/Git依赖，恢复对应输入才能做完整来源审计。

两种运行时本地PG18.6各44项普通回归及core.sql通过，0失败/错误/跳过。候选最终测试文件再次44项通过。新增test_lock_timeout_then_recovery_reuses_settings_plan保持queue行锁直到sender收到Rejected，读取新增日志同worker PID的55P03证明lock_timeout执行，排除57014/客户端超时；释放后同sender发布、收件、ACK、计数归零、worker不重启。它是普通数据库争用回归，没有故障注入。

该正常回归值得保留到七计划PR2；合并三SET的运行时改动不保留。新增测试的[CI36720731289](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36720731289)已核验：Linux/Windows Server2022×PG18/17/16，源码安装与归档重装12轮464项，0失败/错误/跳过；12个core.sql标记及12次55P03日志、12外层附件和6内层候选的全部文件散列通过。head450681d与实际checkout191d994同tree d0abc20；PG18每轮44项、PG16/17每轮36项。详见[可机读验证记录](evidence/spi-plan-ci450681d.json)。不能把此前452项结果说成已覆盖新测试。当前运行时仍470767e，既有六组合452项证据见[上一报告](performance-local-results.md)。安全/parser/fuzz/新故障资格继续暂停，候选仍blocked_security_review，不合并main、不生产发布。

## 后续方向

继续普通SQL计划结构检查，先定位“真正空队列”每次仍执行的两个UPDATE与权限查询，不再重复本次无收益候选。零收件不必等于没有写入：claim可能清理耗尽租约；因此不跳过提交或放宽同步语义，也不假设每次空轮询都刷WAL。对零计数快路径等更大的SQL改动，先评估非空路径增加的成本、完整迁移、正常并发/恢复语义与管理员触发器影响，再决定是否值得做小实验。

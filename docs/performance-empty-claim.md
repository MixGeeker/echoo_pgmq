# 真空队列SQL快路：局部节省、非空代价与实际覆盖率

2026-09-30。**本轮停止产品接入这个zero-count候选，不继续追加它的性能矩阵。** 保留原型和全部证据，产品SQL/原生运行时均未改变，七计划仍独立留作PR2候选。

理由不是“没有任何局部收益”：空队列确实省了SQL批次CPU。但非空路径多一次查询，已有native工作负载中真正可覆盖仅约三分之一claim；整体净收益尚未建立，却要引入SQL迁移和管理员触发器兼容约束。若未来存在高占比空闲订阅的明确需求，可以复用此原型重新评估；当前不为一个低把握净收益继续扩展实验。

## 原型与普通语义检查

[两份完整函数SQL](../bench/experiments/sql-empty-claim/)只装入可丢弃数据库。A是已安装0.1.1的claim原体换名；B在原auth/参数检查及v_now之后，只有在transaction_read_only为off且新查到queue.message_count=0时返回空。没有缓存空结果，没有跳过权限检查，也没有改原生提交或确认语义。

最终完整SQL文件按原样装载验证：A/B各10个普通行为检查，另1个来源核对，共21项通过、0失败，最终计数0/0并正常关停。包括正文/generation/attempts、ACK归零、未来租约和dead保留、真实过期后的64+6清理、同事务流程、两种ROLLBACK、两连接提交可见性，以及A/B只读事务均报25006。

限制不能省略：空UPDATE本来会触发管理员添加的statement triggers，B提前返回会跳过；触发器重入还可能看见同事务中计数尚未更新的中间状态。这个机制原型明确排除这些扩展方式，不能宣称完整SQL语义等价、全面并发/隔离级别或产品兼容。未做产品迁移。

## 微实验方法

- 128.53秒完成，固定BA/BA/AB。每arm每场景先暖5,000次，再测50,000次，共600,000测量调用和60,000暖调用
- 场景一为真正空队列；场景二保留一个未来租约，message_count=1但当前不可领。第二种场景不命中快路，用来测额外点查代价；它不等价于ready-message成功领取成本
- 每批为一个EXPLAIN ANALYZE驱动的SQL语句/外层事务。使用dependent LATERAL确保调用，核对50,000次函数循环、generate_series行数以及单循环Nested Loop总0行，禁止Memoize/Materialize/Gather路径。不能只凭“每循环平均0行”断言总0行
- 每暖/测批次前后核对实际行数、id/generation/attempts/state/owner/lease/available_at/body和计数完全不变；测量结束用原receipt正常ACK，计数回0
- 指标为同一backend的user+system CPU差/调用数，**包括共同驱动、执行器、规划/结果处理及摊销事务开销**。不是纯函数body profiler，更不是原生事务桥/AMQP单消息开销。每批Execution Time也不是单消息p99
- jit=off、parallel=0，避免50,000行人工驱动触发真实单次claim没有的JIT/并行；不是建议改生产配置。5,000次暖调用使之属于暖态机制
- 复用原有PG-only门禁：4次各5秒暖+20秒测；全窗及两个10秒子窗开始/完成≥190TPS，邻接吞吐差≤5%，p99差≤max(1ms,前值50%)，全部通过。门禁位于批次之间，不证明每个批次资源争用完全一致
- 仍为PG18.6、volatile overlay/tmpfs、CPU亲和性而非硬配额。CPU主指标只计指定backend，未覆盖全部server/client进程；4个gate另有216条owned-server资源样本。CPU统计100Hz：1tick/50,000调用为0.2µs，保守配对量化尺度约0.8µs/调用

### 测得取舍

| 场景 | 对 | A CPU µs/调用 | B CPU µs/调用 | 绝对差µs | B/A变化 |
|---|---:|---:|---:|---:|---:|
| 真空 | 1 | 29.8 | 15.8 | -14.0 | -47.0% |
| 未来租约仍保留 | 1 | 28.2 | 34.4 | +6.2 | +22.0% |
| 真空 | 2 | 28.4 | 17.2 | -11.2 | -39.4% |
| 未来租约仍保留 | 2 | 27.8 | 35.0 | +7.2 | +25.9% |
| 真空 | 3 | 28.4 | 20.4 | -8.0 | -28.2% |
| 未来租约仍保留 | 3 | 34.4 | 35.6 | +1.2 | +3.5% |

空队列变化中位数−39.4%，范围−47.0%至−28.2%；未来租约中位数+22.0%，范围+3.5%至+25.9%。后一场景第3对仅+1.2µs，须结合量化和主机波动解释。三对是同环境串行样本，不是统计置信区间。批次EXPLAIN执行时间方向相同，完整数字见[CSV](performance-empty-claim.csv)。

两场景buffer-hit也符合路径变化：每50,000次，A均400,000 hits、B真空350,000、B未来租约500,000；测量批次均无shared read、dirty/write、temp I/O和WAL记录。这不是吞吐结论，也不能假定每个空claim必然flush WAL。原claim返回空仍可能完成耗尽租约清理，不能据此跳过提交或放松同步语义。

## 既有原生trace：真正可覆盖多少

重算此前七计划插桩轮六cell，不重新运行负载。以下表中计数仅属于已核验的native measurement envelope，并非全阶段trace计数。按成功durable publish返回+1、成功ACCEPTED settlement返回−1重建retained；成功claim不改变retained。消息/ID/tag/receipt逐一对应、计数非负、全阶段及native envelope首尾归0，并精确复现每个envelope的空claim数。

| 原生版本 | 全部claim | 空claim | 空且retained=0 | 占空claim | 占全部claim |
|---|---:|---:|---:|---:|---:|
| 原产品A | 52,046 | 34,063 | 16,033 | 47.07% | 30.81% |
| 七计划B | 52,697 | 34,709 | 16,678 | 48.05% | 31.65% |

七计划B的其余18,031次空claim仍有retained，重建时均已被领取但尚未settle；约52%的空领不能走zero-count快路。成功领取和这些正计数空领合计约68.35%的claim仍要多做点查。

关键证据边界：原trace没有直接记settlement outcome。这里的ACCEPTED由固定accept-only消费者源码、客户端无错误、逐ID/receipt/成功操作关联及最终归零共同推定；模型所需证据缺失就应记unknown，而非默认减1。本组六cell在这些闭合证据下unknown为0。不是另做了每次数据库计数采样。[分类汇总与限制](evidence/empty-claim-coverage.json)保留完整汇总。

**不把上述覆盖率乘SQL微实验百分比来宣称MQ加速。** 两轮不同，成本边界不同，而且future-lease代价不能代替ready-message代价。对当前工作负载而言，覆盖率与已观测非空代价不足以支撑直接承担迁移/兼容复杂度。停止的是这次产品接入候选，既有七计划CPU信号并未被否定。

## 完整性与勘误

独立核验80/80原始manifest和201项离线一致性断言通过；后者不是产品测试数。24批次、精确循环/夹具、4gate/3drift均复算通过。监督器未发TERM/kill，PG正常关闭；550秒工作截止及外部控制器边界未触发。所有原始历史保留。

**一处元数据勘误：** 原始installed-provenance.json写extension_binary_recorded_not_exercised=true，范围过宽。enqueue_binary在测量之外建夹具时确实调用扩展C编码器，并非没加载或使用二进制。被测批次是SQL claim副本；native worker、事务桥和AMQP未被基准测试。原始文件未回写，报告明确更正。

完整证据包echoo-empty-claim-micro-and-coverage-evidence.zip已保存交付：1,171,175字节、136成员，SHA256 **f52e8a5d610ddc23c88745d972d58e3587656860dd7672506444590c46851b53**。含原始80项、源码/函数/21项普通检查、smoke、独立复算及旧trace分类脚本；无PGDATA、私钥或实际ERP资料。旧native原始37MB包已在前轮分包交付，本包只引用其hash，不重复大文件。脚本保留原环境路径依赖，不宣称解压即可无依赖重跑。

## 后续优先级与复杂性

| 路径 | 证据/潜在净收益 | 复杂性与决定 |
|---|---|---|
| 七计划复用 | 无native插桩owned CPU下降6.6%–12.8%，尾延迟混合 | 保留PR2审查候选；464项普通CI已验证，不自动采纳/合并 |
| 三SET合一次SPI | 同轮CPU及主要时延均变差 | 已停止，原型/负结果保留 |
| zero-count SQL快路 | 真空节省、正计数开销；仅31.65% claim符合快路 | 本轮不做产品迁移、不继续矩阵；存在trigger语义差异 |
| 既有响应的发送顺序 | 七计划trace中首次后续send晚于成功claim约39%/68%/53%；排队→首次send尝试p95约0.23–0.30ms | 下一窄假设：先做有界非阻塞flush，再领消费者消息；保留每轮预算/SQL/提交。先普通源码审查和正常回归，收益未验证 |

最后一项的“首次send”只是同连接后续首次socket发送尝试，不证明Accepted内容已经出网。机会量级有限，不承诺消除全部确认尾延迟；先验证再决定是否值得更多工作。

所有生产门禁仍在[PROGRESS](PROGRESS.md)：人工安全/相关parser、fuzz、新故障资格继续暂停，Win11/持久SSD/完整升级恢复/真实ERP等未完成，不合并main或生产发布。

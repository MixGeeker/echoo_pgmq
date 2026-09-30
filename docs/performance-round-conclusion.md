# 2026-09-30 性能优化阶段结论与下一步

本轮停止追加没有新依据的微优化配对，保留一个有重复CPU方向信号的候选；接下来优先修复试用配置一致性、明确外部契约，并落实业务短事务集成建议。没有声称整体性能目标通过。已发预览固定450681d/extension0.1.0；main尚未合并，完整安全/故障、Win11/持久SSD与真实ERP资格仍未满足。

## 候选与取舍

| 路径 | 已观察到的结果 | 本轮决定与代价 |
|---|---|---|
| 七条SPI计划复用 | 无原生插桩3对，自有PG采样CPU −9.29/−6.59/−12.77%，worker −11.43/−7.92/−15.05%；第2对确认/收件E2E/DB p99 +16.81/+6.76/+31.17%，绝对 +0.439/+0.276/+0.643ms | 保留PR2审查候选，已用于隔离预览；464项普通矩阵通过。不把CPU信号升级为稳定尾延迟或整体性能提升 |
| 3个SET合为一次SPI调用 | 三对自有PG CPU +15.89/+6.91/+2.63%，主要时延分布变差 | 不采用；减少调用数本身不是收益 |
| 真空队列zero-count快路 | SQL批次真空CPU −28%至−47%，未来租约仍保留时 +3.5%至+25.9%；native仅31.65%全部claim可命中 | 不接入；额外查询、SQL迁移及statement-trigger语义代价，没有AMQP净收益证据 |
| 提早发送已有响应 | 唯一有效对确认p99 −14.70%，但收件/DB p99 +5.31/+4.23%，worker CPU/收件 −1.46%；G2环境漂移后正常停止 | 隔离patch；P2无效、P3未跑，不重试刷绿，不扩成产品改动 |
| SQL0.1.2容量预留 | 普通矩阵704次通过；原生worker CPU/收件 +8.62/+3.65/−0.76%，DB p99三对升高；autocommit WAL −54字节/消息（−3.35%） | 保留PR3研究，未进入预览；单外层事务批次CPU下降不能外推AMQP。涉及版本化SQL迁移，收益不足以承担升级成本 |
| 发布后唤醒/剩余deadline夹紧 | 旧实验尾延迟与CPU混合，独立deadline臂空claim/消息增加；新trace仅定位等待机会，未证明净收益 | 唤醒已撤回；deadline复核后不重开。旧源码仅有hash，未据此假称条件完全相同 |
| WaitEventSet复用 | 可见setup/free合计仅约1.56%被观测wall时间，是宽上界而非CPU可省比例 | 低优先且Windows句柄生命周期风险高，没有新证据重开 |

详细数字及坏结果：[七计划](performance-local-results.md)、[SET合并](performance-batched-settings.md)、[空claim](performance-empty-claim.md)、[early flush](performance-early-flush.md)、[SQL0.1.2](https://github.com/MixGeeker/echoo_pgmq/blob/7eb7409a75a9b80a4d1460cd099c434c37bdddc1/docs/performance-sql-reservation.md)、[deadline历史复核](performance-deadline-reassessment.md)。百分比为同轮同对B/A−1；三个短时样本不是统计置信区间。

## 剩余有依据的工程方向

1. **先完成预览入口配置与版本化契约。** 普通静态核对发现.1的AMQP完整编码上限65,536字节、SQL默认1MiB；同一合法消息可能SQL成功而AMQP领取转dead。准备.2使用同一常量统一新测试库全局、包装器队列与worker限额。SQL计量的是最终编码bytea，与worker大小对象一致；无需猜测body固定开销。投入限于包装器、静态回归与Linux/Windows18合法小消息验证，直接收益是消除试用入口尺寸行为错位，不承诺性能加速。原.1字节保留，.2发布前单独验证；详见[当前.1契约](preview-20260930.1-contract.md)。
2. **将业务短事务使用规则落实到后续ERP接入。** 已证明SQL enqueue持有全局容量锁150ms时，另一队列正常发布/远端消费结算约157–158ms；这里的150ms是人为业务事务等待，不是ERP计算耗时。业务逻辑允许时把enqueue放在事务末尾、紧接COMMIT，统一业务行→容量行锁顺序，减少持锁时网络往返和外部等待。仍保留业务与消息原子提交；需真实调用方评估是否依赖返回message ID、触发器或后续更新。投入主要在调用链检查和代表性集成验证；尚未实测这一重排的收益，不能把2ms无争用值当改后承诺。[完整争用证据](performance-global-contention.md)

更多worker不能消除同一严格总容量行的串行化。预分配/分片额度可能减少热点，但牵涉额度利用率、迁移、恢复与回收；不在这一轮当成低风险内部优化，也不以近似计数、提前Accepted、异步容量回收换分数。公开batch API更会改变外部契约，当前没有据此扩展接口。

## 已知成本与本轮边界

桥分阶段诊断唯一有效对中，query+data占已计时桥操作elapsed的86.68%，begin为5.71%，commit为7.61%。它不是整个worker CPU归因；只凭这个比例不能选定下一SQL改法。计时B本身未证明无扰动，G2漂移后P2无效，所有结果保留。空闲8连接短测worker约单核1.242%，全PG采样CPU较前后基线增加1.248/1.402个百分点；不推断长期内存、容量或峰值伸缩性。[分阶段](performance-bridge-phases.md)、[空闲资源](performance-preview-idle.md)

云环境workspace为overlay fsync=volatile，临时目录为tmpfs；本机实验只有CPU亲和性，没有整栈硬内存/CPU配额。PG的同步提交参数保持开启，也不能把易失介质结果叫SSD耐久性能。此前GitHub长期比较的固定2CPU/4GiB服务栈和不同runner结果独立保留，不能拼成同机严格A/B。

200/400msg/s、1KiB是测试点，不是最大吞吐；逐条确认的闭环负载会在变慢时少发。实际门店峰值未知，2倍峰值无持续积压尚未验收；ERP/DB p95相对增幅≤10%是暂定目标，历史合成比较未达，不能改阈值掩盖失败，也不能把亚毫秒合成基线的相对变化当真实ERP结论。分层目标仍是[待确认草案](performance-acceptance-draft.md)，未新增用户批准的SLO。

这轮没有新的、证据足以支持接入的运行时性能改动。下一步以可用性修复与契约落实为优先，后续性能工作需新的代表性输入或明确机制证据；不为“持续优化”重复已否决路线。完整审核和发布门禁见[PROGRESS](PROGRESS.md)。

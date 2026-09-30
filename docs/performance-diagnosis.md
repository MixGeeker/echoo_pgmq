# 普通性能长尾诊断：现有证据与最小下一步

日期：2026-09-30。范围：只读源码、历史原始数据与官方 API 文档；未改运行时代码，未执行新 benchmark、抓包、提权、故障注入、安全或 parser 调查。

## 结论先行

**本轮不能把长尾归因于 wake 回归。PG-only 自身已无法维持 200 TPS，是首要混杂因素。保守回退的理由是没有足够稳定收益证据，并非已经证明代码故障。**

当前最强线索是“PG/运行环境的长事务或共享停顿，可能经 native 的同步事务和单线程循环传导/放大”。Accepted 排队后再执行 claim，是可以从源码确认的额外放大路径；TCP 小包合并也值得保留，但原始直方图没有显示明显的 40ms 固定阶梯。三者并不互斥。

不建议直接再跑完整矩阵。下一步应先通过 PG-only 稳定性门槛，再做同一机器、短窗口、交错随机顺序的 A/B，并补齐阶段计时。

## 1. 数据与时钟先验已复核

读取两份已验真的完整原始artifact：
- [旧运行36663717022](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36663717022/artifacts/11077458716)，构建提交`51ca717`
- [唤醒运行36672765081](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36672765081/artifacts/11080649776)，提交`472e626`

只在各 cell 的 `measurement_start_ns ≤ start_ns < measurement_start_ns + duration` 内计算下文性能统计；全阶段对账另包含 warmup/drain。

全阶段逐 ID 重新核验结果：

| 检查 | 旧 run | 新 run |
|---|---:|---:|
| producer / consumer 匹配条数 | 1,077,932 | 1,000,146 |
| producer 或 consumer 重复 ID | 0 | 0 |
| 缺少对应 consumer / 无对应 producer | 0 | 0 |
| receiver.sent_ns ≠ producer.start_ns | 0 | 0 |
| 负 publish / E2E 时长 | 0 | 0 |
| 用双方时间字段判定测量窗口的差异 | 0 | 0 |
| E2E 与 `(received_ns−producer.start_ns)/1e6` 重算最大差异 | 0 ms | 0 ms |

`bench/run_duration.py:86–98,112–140,154–160,292–311` 中 producer、consumer、synthetic DB 都是同一 host 的 multiprocessing 子进程，使用 `time.perf_counter_ns()`；公共 epoch 由父进程共享。两份环境记录均为 Python 3.12.14。Python 3.12 文档明确该计时器是 system-wide，且返回值只用于差值。因此现有 E2E 和客户端 DB/confirm 重叠不是跨机器绝对时钟相减。[Python 官方说明](https://docs.python.org/3.12/library/time.html#time.perf_counter)

这不证明任何服务器阶段对时：原始数据没有服务器 publish/claim/commit/Accepted-write 时间戳，也未记录当时 `get_clock_info()`。当前无法把客户端总时长拆成服务器与网络的精确占比。E2E 的终点是应用读取消息后的时间采样，早于随后 checksum 校验和 consumer ACCEPTED，不是 consumer ACK 持久化完成。

## 2. PG-only 混杂与闭环发压

| PG-only | 旧 TPS / p99 ms | 新 TPS / p95 ms / p99 ms |
|---|---|---|
| r1 | 199.883 / 1.695 | 172.717 / 0.824 / 19.743 |
| r2 | 199.958 / 1.520 | 165.242 / 0.904 / 37.002 |
| r3 | 199.942 / 1.513 | 170.183 / 0.797 / 24.512 |

新基线三轮只有目标的 82.6%–86.4%。p95 尚好不能使这个基线有效；慢事务占比较低，却耗掉大量墙钟时间。新 PG-only 的 >20ms 事务分别 207/263/225 条，覆盖各测量窗口约 14.0%/17.8%/15.3% 的时间。

producer 每连接仅 1 条 in-flight，BlockingSender 等对端 settlement 后才返回；DB 也串行逐事务。二者均使用 `due=max(due+interval,complete)`，不补发积压的计划到达。因此这是有固定目标的闭环 pacing，不是独立于服务响应的开放式到达压力。小 schedule_lateness 也不能证明目标到达已实现，因为 due 本身被重置。

一个直接核验：新 PG-only 三轮 `sum(max(DB_latency−5ms,0))` 为 **16.427/20.826/17.865 秒**；实际少做的事务按 200 TPS 折算为 **16.370/20.855/17.890 秒**。二者几乎一致，余差含窗口边界与计时/pacing 开销。长尾导致少发足以解释目标缺口，不需要假设时间戳错误。

环境记录还显示 runner image 从 `20260920.314.1` 换到 `20260927.320.1`。包/配置相同不等于宿主存储、调度或虚拟化噪声相同；也不能反过来仅凭 image 版本变化断定原因。没有 OOM/CPU quota throttle 不排除 I/O 或宿主调度停顿。

## 3. 已有时间关联比“约 40ms”更有辨别力

方法：每个 Echoo cell，取 confirm >20ms 的 producer 区间 `[start_ns,complete_ns]`，判断是否与同 cell 的 synthetic DB >20ms 区间相交。只在客户端公共时钟域比较。

- 新 run：370,285 条测量期 Echoo publish 中有 6,474 条 confirm >20ms，其中 **6,024 条（93.05%）**重叠 DB 长事务；各 cell 为 80.88%–98.00%
- 将相同 producer 区间平移 5 秒并按 120 秒窗口循环折回，重叠变为 **1,995/6,474（30.82%）**，各 cell 为 3.00%–56.53%
- 旧 run：430,651 条测量期 Echoo publish 中只有 4 条 confirm >20ms，均在同一个 cell，且都重叠该 cell 唯一的 >20ms DB 事务

平移只是保留尾长/事件密度的描述性对照，边界不作正式统计检验；长区间本来更容易重叠，周期性负载也可能影响平移结果。结果支持共享停顿线索，**不证明是 fsync、checkpoint、TCP 或某条 SQL 导致**。

新 run 的 Echoo confirm >20ms 分布：20–35ms 1,602 条；35–55ms 1,221；55–75ms 844；75–100ms 962；100–150ms 775；≥150ms 1,070。只有 575/6,474（8.88%）位于正 40ms 整数倍 ±2ms。各组 5ms 分箱众数均是 20–25ms，而非 40/80/120ms。它是宽尾，不应从四个 p99 中取整后认定 delayed ACK 指纹；TCP 与其他阶段叠加时仍可能不呈规则阶梯。

新 Echoo 同 ID 的 `received_ns−confirm_complete_ns` p99 在全部 12 个 cell 中仅 0.258–2.900ms，旧 run 为 2.786–4.274ms。两种客户端终点在新 run 更接近，符合“确认和投递一起被更早的共同步骤阻塞”，也符合“wake 提前了投递，但确认被同轮工作拖后”；它不是 Accepted 已经立即上网的证据。

Rabbit 的 synthetic PG 也出现 p99 高至 43.522ms，而消息 E2E p99 仍约 1.2–3.29ms。这削弱“所有 broker 网络流量一起出现同样长尾”的泛化，但 Rabbit 消息路径不依赖该 synthetic PG，不能据此排除 native 的 PG/WAL 路径停顿；不同 cell 也不是同一时刻的严格对照。

## 4. 三个竞争机制

### A. PG I/O / 环境停顿，native 同步路径传导（目前证据最强）

`472e626:src/storage.c:18–41,93–124,148–213` 表明 publish、claim、settle 都在 worker 内同步执行各自事务；publish 的 Accepted 只在 `CommitTransactionCommand()` 返回后排队。一次慢 publish 直接拖慢 confirm，一次慢 claim/settle 也会阻塞整个 AMQP worker 的其他连接服务。

PG-only 异常和 93% 的客户端时间重叠支持优先检查这个方向，但尚不能细分 WAL sync、其他 I/O、锁等待、进程调度或宿主停顿。现有资源采样粒度/累计 I/O 字节数不足以识别几十毫秒阶段。PG18 的 WAL write/fsync 次数和时间位于 `pg_stat_io` 的 `object='wal'`，时间字段需相应 timing 设置，不能误用旧版 `pg_stat_wal.wal_sync_time`。[PG18 WAL 官方说明](https://www.postgresql.org/docs/18/wal-configuration.html)

### B. Accepted socket-write 顺序被同步 claim 延后（源码可行，代价未测）

`472e626:src/echoo_pgmq.c` 的正常路径：
1. `receive_message:380–396`：publish commit → `pn_delivery_update(PN_ACCEPTED)` → wake → settle/flow；这里没有 socket send
2. `main:847–874`：仍遍历所有连接、处理事件和 `pump_sender`；`pump_sender:608–609` 同步 claim
3. `main:886–904`：全部完成后建立 wait set；`service_io:686–693` 才调用 `send()`

因此 publisher 后面若还有被唤醒的 consumer，claim 可以发生在 queued Accepted 的首次 socket write 之前。旧代码也可能执行已到期 claim；wake 增量是把原本未到期的 eligible consumer 强制变为 due，从而增加当轮工作的机会。不能把所有轮内 claim 开销都归于新补丁。

新连接在 `accept_connections:743–744` 头插；先 accept consumer、后 accept publisher 时，遍历次序为 publisher→consumer，容易出现此路径。benchmark 先 spawn consumer，但没有逐类 readiness 屏障，因此不能推定每个历史 cell 的实际 accept 次序。还有其他连接事务及输出队列顺序，必须记录实际顺序后判定。

Proton driver 本身缓冲事件/字节；获取 write buffer 与调用 IO write 是不同步骤，Accepted 状态更新不等于已写出。[Proton 0.40 官方 driver API](https://qpid.apache.org/releases/qpid-proton-0.40.0/proton/c/api/group__connection__driver.html)

### C. native server 的 TCP batching / Nagle（保留，尚无因果证据）

`accept_connections:704–719` 只做 nonblocking；`create_listener:763–774` 的 socket options 是 REUSEADDR/EXCLUSIVEADDR/IPV6_V6ONLY，没有 TCP_NODELAY。仓库 baseline 与 wake 两版均如此。实际安装的Python Proton0.40.0包内 `proton/_io.py:36–38,60–64` 则在客户端 connect 前设置 `TCP_NODELAY=True`。客户端设定不会替代 server socket 的设定。

未在 native accepted socket 上实测 `getsockopt`，因此严格结论是“代码没有启用”，而非已验证所有运行时 socket 的值。小写入/TLS records 与未确认数据相遇时，Nagle/ACK 交互可能造成等待；`send()` 返回仅代表内核接收字节，不等于已到对端。关闭 Nagle 是每 socket 的选择，不需要系统级网络调整。[Linux TCP 官方手册](https://man7.org/linux/man-pages/man7/tcp.7.html)

该缺项在旧版也存在、旧 run 尾延迟很少，且新直方图不呈明显 40ms 阶梯，所以不能单独解释新旧差异。wake 改变写入节奏时与 TCP 行为交互仍可能存在。实际 benchmark 的 poll interval 是 **2ms**，不是源码默认 50ms。

## 5. 最小、有限的下一步设计（仅提案，尚未执行）

### 固定范围与停止条件

- 一台相同 runner、同一 session，固定 CPU/内存/存储/包/config/客户端；不同时跑 A/B，不跑 Rabbit 或完整矩阵
- A = baseline runtime（96c18db/a501852；当前 37fc015 已回退至该 runtime），B = 472e626 wake；两者只差 wake，使用完全相同的低开销诊断观测
- 仅 1 producer / 1 consumer、1KiB、200 msg/s、synthetic DB 200 TPS、相同 mTLS/credit/持久化/settlement；这是机制诊断，不能推出容量结论
- 3 个相邻配对，每对随机为 AB 或 BA；顺序种子预先保存，且整组同时包含 AB 与 BA。每 cell 5 秒 warmup + 30 秒测量 + 有界 drain；保留各 cell 原始数据，不只汇总 p99
- PG-only G0 → 第1对 → G1 → 第2对 → G2 → 第3对 → G3；每个 G 5 秒 warmup + 20 秒测量。计时负载共约 310 秒，容器准备/drain 另设总预算（建议 10 分钟封顶），到期输出诊断不足，不追加长跑
- 相邻 A/B 必须验证相同连接遍历次序。可在测试客户端加入一致的 consumer-ready→producer-ready 启动屏障并记录实际 accept/连接顺序；历史 spawn 次序不能替代这个观测

### PG-only 稳定性门槛，先判再比较

这组门槛只约束未来短诊断的可比性，不替代业务容量或ERP p95≤10%的候选目标，也不倒改两份历史原始数据。

预先固定门槛：G 的整体和两个 10 秒子窗均 ≥190 TPS（200 目标的 95%），无事务/采样错误；同时报告 p99、>20ms 比例、`sum(max(latency−5ms,0))`。前后 G 的吞吐差≤5%，p99 变化≤`max(1ms,前窗p99×50%)`，作为预先声明的漂移筛查，而非统计显著性证明。

G0 不达标：不启动 A/B，结论为 **环境门槛失败，仅诊断有效**。后续 G 不达标/明显漂移：相邻配对无效，停止后续负载，不把失败窗口挑掉后只展示好结果。单独 A/B cell 的 achieved rate 不足 95% 时仍保存阶段证据，但不能宣称有效的固定 offered-load 延迟对比；不能借闭环少发证明 backlog 稳定或容量充足。

### 必需观测与判别结果

共同观测只针对测试自身：服务器单调时钟下的 publish start/commit return、Accepted 入队、claim start/commit return、settle start/commit return、连接/loop 次序、首次及后续 socket write 的长度/返回值/时间；按测试 delivery/tag/序号关联，不记录正文/凭据。用内存缓冲，测量后落盘，避免逐事件同步日志制造 I/O。每个 owned socket 记录实际 NODELAY；可读取本 socket 的 TCP_INFO 可用字段，无抓包、无特权 trace。

客户端继续记录原始 send/receive/DB 时间，额外分别记录 synthetic DB SQL 与 commit 的时长、clock implementation/resolution。服务器内部只先比较同一进程内的阶段差值；若要跨客户端/服务器做绝对时间关联，先显式核验公共时钟来源/时间命名空间，否则通过消息/序号关联，不能直接相减。

已有权限可读时，记录测试 backend/worker 的 wait_event、PG18 `pg_stat_io`/checkpointer 差值及测试进程/cgroup 的 I/O/调度指标；若 timing 未启用或不可读就明确缺项，不为此提权或更改 OS 设置。官方说明指出 timing 本身可能有开销，A/B 必须一致。[PG18 统计设置](https://www.postgresql.org/docs/18/runtime-config-statistics.html)

判别：
- **PG/环境主导**：publish/claim/settle 的 commit 或其他 DB 阶段吞掉尾延迟，且与 synthetic DB/可见 WAL 或 wait 指标共现；Accepted 入队→socket write 的额外间隙短，A/B 差异不稳定。此时先解决环境/DB观测，不提长跑或性能胜出
- **write-order 放大**：B 中 Accepted 已入队后，后续 claim 明确位于 socket write 之前；其累计时长解释相当一部分额外确认延迟，A 同阶段较短且配对方向一致。这能确认放大路径被触发；要证明收益/回归仍需配对效果和稳定基线。后续若获准，只做“先冲刷已排队 Accepted、再 claim”的单因素小对照
- **TCP/IO 出栈后主导**：服务器 DB 与入队→write 均短，但客户端确认仍长；本 socket 的未确认数据/发送状态支持等待。客户端调度/Proton 事件消费仍是竞争解释，不能只凭这一步锁定 TCP。后续若获准，仅对 native accepted socket 做 NODELAY 单因素短对照，固定 wake 和 write order，绝不同时改三项

这轮的完成条件是“稳定门槛通过后的阶段归因线索”，或“门槛失败/样本不足且如实停下”；不是必须找到一个代码罪因。当前不恢复 wake、不改变网络设置、不启动第三轮完整矩阵。

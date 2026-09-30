# 一次性普通长尾机制诊断

仅在 `diagnostics/ordinary-tail-20260930` 上准备和执行；不并入产品，不打包候选，不改变任何产品运行时代码、历史原始证据或性能目标。参考 `docs/performance-diagnosis.md`。本轮以得到有限机制线索或如实说明环境/证据不足为结束条件，之后关闭这轮性能研究，不重跑至通过。

## 预先固定的范围

- A：`6867f2b` 中已恢复的 baseline native；B：`472e62698137d0e1035d04775d443f1cbcc1ae33` wake native。公共 CMake、头文件、SQL、其余源文件全部取 A
- 生成器对 A/B 执行同一组可逆文本插桩，验证两棵生成树只有 `src/echoo_pgmq.c` 不同；去掉插桩逐字节回到两个固定原文。原始 native 哈希、完整源文件哈希、插桩哈希均保存
- 两侧在一个不可变镜像内、相同编译选项下构建，PG18.6、Proton0.40.0、RelWithDebInfo、test hooks OFF。同一 Linux GitHub runner/session 串行执行，服务器共 2 CPU / 4GiB / 0 swap，客户端与采样器在另外 2 CPU
- 新建 volume/容器以隔离每个 stage 的队列和表；保留宿主缓存，不清缓存、不推断物理 SSD。不是专用宿主或 Windows/ERP 证据
- 仅 1 producer / 1 consumer、1024 bytes、200 msg/s、合成 DB 200 TPS、mTLS + EXTERNAL、credit32、durable + 等待 Accepted、手动消费 Accepted、单连接 1 in-flight、poll2ms、正常同步事务
- 同样启用 PG `track_io_timing` / `track_wal_io_timing`，观测开销是诊断条件的一部分；不是无插桩产品性能测试

种子固定 `20260930`。Python 3.12 的 `random.Random(seed).choice` 从全部六种同时含 AB/BA 的三对顺序中选一次，得到 **BA、BA、AB**。不重抽、不根据结果重排。运行前将完整计划写入 `plan.json`：

`G0 → P1-B → P1-A → G1 → P2-B → P2-A → G2 → P3-A → P3-B → G3`

G：5s warmup + 20s measurement；cell：5s + 30s。总计时负载 310s。每 stage 的 drain 最多5s，客户端连接/操作 timeout3s。consumer remote link-open 完成且 readiness 已记录后才启动 producer 进程，两侧一致；服务器真实 accept/遍历顺序必须被 trace 再验证。

## 门槛与停止规则

- 每个 G 的全20s及两个10s子窗，按开始/完成两个口径均至少190 TPS，零事务/采样错误；缺样/未完成窗口失败
- 报告 p99、>20ms 比例及 `sum(max(latency−5ms,0))`
- 相邻 G 的吞吐变化不超过5%，p99 绝对变化不超过 `max(1ms, previous_p99*50%)`
- G0失败不启动任何 A/B。后续 G 失败或漂移：作废邻接刚完成的配对并立即停止，后面未跑 stage 明列，不丢弃失败数据
- cell 的发送、接收、合成 DB 任一实际负载低于95%时仅为机制诊断，不构成有效固定负载比较。没有因此追加数据或改变指标
- trace 缺失、未正常落盘、计数溢出、时钟/身份/观测错误、逐ID路径缺项，或实际连接顺序不符时停止为诊断不足
- 终态包含 `completed`、`completed_diagnostic_only`、`environment_insufficient`、`diagnostic_insufficient`、`budget_insufficient`、`infrastructure_insufficient`，均不等于产品性能/可靠性验收

控制器预算覆盖镜像解析、容器准备、客户端负载、尝试正常清理和证据保存，**硬上限600s**；这不等于承诺服务器进程或容器的绝对存活时间不超过600s。工作截止550s，预留50s供正常停止/证据。每次启动 stage 前必须满足其计时负载+25s准备/退出时间；不够就不开始。所有外部命令 timeout 限于剩余预算，另有独立599s硬截止处理器，先写小型budget_insufficient终态、再直接退出，不能被异常捕获吞掉；清理命令最迟595s终止，余5s写终态。若硬截止触发，旧manifest撤销并明确标记不完整，保留所有原始部分文件。超时只结束自己创建的客户端进程组，不对 PG 做 crash test。

Docker stop 使用 `--time=-1`，不自动转成强杀；CLI等待有界。599s fail-stop约束控制器并终止仍在发压的自有客户端进程组，但可能留下已经停止或仍在正常关闭中的自有容器。正常清理未能完成时会保存 `cleanup-incomplete.json`；硬截止路径则在终态保留 `cleanup_may_be_incomplete=true`，由 `active-unit.json` 与对应 `.stack/lifecycle.json` 找到容器名。必须将这项清理作为待办继续核实，只做正常停止/删除已停止容器，不强杀PG、不重跑负载，不能把控制器退出当作服务器已停止。镜像构建单独上限900s，记录 `build-status.json`，不计入310s负载或600s控制器。工作流job上限40分钟，checkout/setup各2分钟、验证3分钟、构建16分钟、控制器步骤11分钟、上传5分钟，共39分钟并留1分钟调度余量；这些余量不放宽900s构建或600s控制器内部预算。

## 实际加载的 arm

镜像里 A/B 二进制分别为 `/opt/arms/{A,B}/echoo_pgmq.so`，并保存 SHA256。真正的 `$libdir/echoo_pgmq.so` 是构建期创建、指向 `/state/selected/echoo_pgmq.so` 的 symlink。非 root supervisor 只向自己容器的私有 state 目录复制选定 arm，随后才启动 PG。因此 worker 的 `bgw_library_name='echoo_pgmq'` 和 preload/SQL helper 使用同一所选库，无需在启动时修改 root-owned pkglibdir。

前后快照从 `pg_stat_activity` 找到本 disposable PG 的唯一 worker，读取其 `/proc/PID/maps` 中 native 库条目，核对真实路径、设备、inode及对应文件SHA256，并与不可变镜像 arm 哈希比较。trace PID必须与该worker一致。请求的arm标签本身不是实际加载证据。

## 有限观测、明确开销与解释限制

- Linux-only native 缓冲区固定1,048,576条，启动监听前分配并预触页。最终Linux构建每条248 bytes，额外常驻观测缓冲区为 **260,046,848 bytes，即248MiB**，计入同一个4GiB cgroup；实际sizeof/容量/总字节亦写入trace metadata。它会占用内存、影响缓存和内存带宽，未测量无插桩对照开销，不能称为“低开销”或推断这些时延等于产品时延
- 容量选择是为了容纳35s中约7,000条消息及更多的loop/connection/wait/send/claim事件；每2ms轮询循环又可能产生多条记录，131,072条没有足够的设计余量。1,048,576条是预先固定的有界余量，并非已证明35s必不溢出的数学上界；若溢出仍立即判证据不足，不扩容重跑。最终冒烟已使用此容量，之后不更改内存或插桩语义
- 两侧每条事件使用相同插桩，但wake会改变循环次数、claim次数与总事件数，因此时钟调用、只读socket查询和内存写入的总观测成本可能不同，不能宣称观察者影响相互抵消。最终短冒烟全生命周期A/B分别记录21,041/19,873条事件，仅证明该短路径可采集，不能外推开销比例。PG-only门槛没有native worker及其248MiB缓冲区，仅筛查环境稳定性，不代表与消息cell相同内存占用。所有比较只限“这一固定插桩条件下的机制诊断”
- 无逐事件文件I/O；只在正常 worker stop 后一次落盘。profiling clock和只读socket检查保留 errno。bounded drop/clock/identity/observation计数，任何非零都使证据不足
- 记录 publish/claim/settle调用开始和返回、Accepted排队、transfer排队、loop/connection顺序、每次socket send开始/返回/长度/errno/sequence及本socket实测NODELAY。不改变NODELAY
- 以唯一受控 AMQP link name + exact delivery tag关联客户端message ID与native connection/link/delivery；consumer delivery同时关联数据库id/generation。不解码/存储正文，不存身份、密钥或SQL文本
- 客户端保存自己socket端点。Docker端口转发可能变换端口，不能假定客户端和服务器端口一一相等；精确关联使用AMQP link/tag，分别保留端点观测。直接同网络命名空间冒烟另验证端口相等
- Accepted queued 不等于send；首次后续send可能包含更早frame、TLS framing或部分write。`queued→first-following-owned-send`只是服务器观察到的间隙，**不是该Accepted的确切出线时间**，更不是对端收到时间。所有后续send原始记录保留
- native插桩使用明确CLOCK_MONOTONIC，PG原有now_ms继续原本instr_time实现，不改调度语义。保留构建`instr_time.h`、客户端clock implementation/resolution、可用的time namespace资料；只计算native自身阶段差，借ID与客户端关联，禁止跨clock直接相减
- DB客户端分别记录BEGIN、SQL、COMMIT耗时。保留PG18 `pg_stat_io`、checkpointer、timing配置的前后快照，按0.5s读取本测试PG的wait_event；原HTTP Docker/cgroup sampler继续采样。缺失字段明确标注，不套用PG17的wal_sync_time字段
- PG/共享环境、Accepted后同步claim延迟socket write、TCP batching/客户端调度仍为竞争解释。短诊断不能证明容量、真实ERP影响、checkpoint长期行为或故障可靠性

## 文件和可复现命令

- `policy.py` / `test_policy.py`：固定计划、门槛、终态与fail-fast确定性测试
- `controller.py` / `test_controller.py`：预算、Docker生命周期、采样、完整证据、未跑stage
- `client.py`：从原`bench/run_duration.py`独立派生，加入readiness、SQL/commit、时钟、link/tag及wait观测；原文件未修改
- `server.py`：从原`bench/ci_server.py`独立派生，选择私有arm、PG统计与实际加载readback；只接受baseline/echoo两模式
- `instrument_native.py` / `native_trace.h` / `test_native_trace.py`：同一native插桩、源证明、完整trace与逐ID验证
- `build_image.py` / `Dockerfile`：同一镜像双arm和独立构建预算
- `smoke_local.py`：仅0.2s+1s/arm的正常全路径冒烟，不运行门槛或本轮诊断

```
python -m unittest discover -s bench/diagnostic -p 'test_*.py' -v
python bench/diagnostic/build_image.py --tag echoo-tail:FIXED_SHA --output /new/path/build
python bench/diagnostic/controller.py --image echoo-tail:FIXED_SHA --output /new/path/result
```

只对最终review过的diagnostic分支推送一次，或在该分支显式dispatch一次，二者不要都做。工作流拒绝其它分支和run_attempt>1；不触发main/PR/security/qualification、Rabbit矩阵、发版或合并。

Artifact `pg18-ordinary-tail-<sha>`（30天有效）包括build log/状态/源哈希、immutable image ID、计划/seed、环境和budget、每stage客户端原始jsonl.gz、summary、readiness、clock、真实HTTP/cgroup/wait采样、PG前后快照、真实加载库证据、正常停止后native trace、逐ID关联分析、终态与unrun列表、全文件SHA256清单。**不导出PGDATA、测试私钥、证书或二进制**。保留所有失败/部分结果；过期前需保全完整artifact。

# 性能验证、对比与持续目标

本页区分实测、候选目标与尚未验收事项。脚本不会关闭 `fsync`、`full_page_writes` 或同步提交来提高成绩。任何短时云端结果都不能作为门店 Win11 性能承诺。

## 新增：同一总预算的普通负载实验

`.github/workflows/benchmark.yml` 只允许手动执行，以及已明确授权的专用分支 `benchmarks/controlled-pg18-20260930` push。普通 PR/main push 不触发，不调用 security/qualification、异常输入、模糊测试、故障注入或崩溃测试。使用标准公共 `ubuntu-24.04` runner，不申请付费大规格 runner。

这是**固定到达率、有限时窗的普通负载实验**，不是最大容量搜索、24小时长稳、真实ERP或门店硬件验收。文件存在不等于实验已通过；在获得并审阅 GitHub artifacts 前，不填写新的性能结论。本机短时验证只证明新客户端能正常工作。

完整矩阵前必须先通过**真实Docker基础设施冒烟**：同一image ID/预算，baseline、echoo、RabbitMQ各一个单元，2秒预热+5秒测量，消息单元为1×1、100条/秒。它实际覆盖HTTP资源采样、cgroup读回、两侧mTLS/发布/消费、数据库采样与正常清理；任何失败立刻停止，不进入27单元。冒烟证据立即单独上传，明确标记`infrastructure_smoke`，不参与性能比较。

首次容器尝试[run 36662006820](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36662006820)已完成镜像构建和27次实际预算读回，但HTTP采样客户端误用Python上下文管理器，全部在预热前停止、测量样本为0。[原失败artifact](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36662006820/artifacts/11074643223)保留；它不能说明任何代理性能或ERP目标。代码已改为显式关闭连接并加回归测试，待新的真实Docker冒烟和完整矩阵验证。

### 资源与持久化

- 每单元新建一个容器和磁盘数据卷；**整个服务器栈共用2个逻辑CPU、4GiB内存、0 swap**，PID上限512。echoo一侧是PG+原生worker；RabbitMQ一侧是PG+RabbitMQ/Erlang。不能给每个服务各配一份预算，或排除额外代理的资源
- 两个服务器CPU ID固定；Proton发布者、消费者、合成数据库客户端和采样进程在另外两个CPU ID上，**在服务器预算之外**。runner至少有4个可用逻辑CPU，不满足直接停止
- 所有单元用同一个不可变本地image ID。基础RabbitMQ4.0.5镜像固定到官方digest `sha256:82ee1d53d63c646b2fbaacfe4c5fb0e01a447047df585d16836ae236637bb15a`；PostgreSQL18.6源码SHA-256固定；Proton C/Python均0.40.0。保存实际image ID、发行版包版本、Python依赖、Git SHA及工作树状态、runner image、Docker版本。apt包没有历史快照，不声称跨日期逐字节可重建
- `docker inspect` 与容器内 cgroup v2 的 `cpu.max`、`memory.max`、`memory.swap.max` 双重验证预算，保存原始CPU/内存/I/O/PID采样。cgroup内存含计费文件缓存，与旧版RSS求和不同
- 两侧PG同设：`fsync=on`、`full_page_writes=on`、`synchronous_commit=on`、`wal_level=replica`、`shared_buffers=512MB`、`max_connections=40`、`max_wal_size=1GB`、`checkpoint_timeout=5min`、`autovacuum=on`。echoo队列表必须是普通WAL表；RabbitMQ必须为单节点durable classic队列。没有复制/quorum或真实断电保障的等价性声明
- RabbitMQ Erlang scheduler固定2、内存高水位512MiB，仍受同一个4GiB总上限约束。echoo轮询2ms、visibility60s、每link最多32inflight。两侧同用1KiB正文、mTLS、服务器名称校验、EXTERNAL、每发布连接一次一条未确认消息、等待远端Accepted、消费者手动ACCEPTED、credit32
- 队列数据使用同一runner的Docker volume后端，非tmpfs；不清空宿主缓存。无物理SSD/易失缓存资格证据；干净GitHub VM仍有共享宿主、虚拟磁盘和调度噪声，不等于独占裸机

### 矩阵与口径

默认3轮，每轮先跑同样的合成PG基线，然后交替代理、连接数组和速率的顺序。每个单元新数据库、新队列、新连接，**连续预热30秒，再测120秒**，最后最多30秒排空。共3基线+24消息单元，预热/测量共67.5分钟；含构建和启停预计80–105分钟，job上限120分钟。

- 1发布者×1消费者、4发布者×4消费者，每个客户端独立进程/连接，避免把单个Python解释器GIL误作服务器瓶颈
- 总目标到达率200、400条/秒，平均分摊到发布者；不同连接数保持相同总速率。这是公开固定的实验条件，不是业务峰值或容量承诺；不持续增加压力寻找饱和/崩溃点
- 发布者按固定间隔发送，等待Accepted后再发下一条；落后时不补发积攒的突发。保存实际尝试率、目标达成比例和调度延后。**跟不上目标速率时，不能把更低的实际负载说成达到了目标**。有界闭环发布模式不能推断异步流水线容量
- 预热/排空原始记录保留，但延迟摘要只取测量窗口内开始发送的消息，其窗口外迟到完成仍计入延迟。端到端延迟是发送开始到校验通过的接收时刻，不是消费者ACK提交耗时。吞吐按固定120秒内Accepted/唯一接收事件计算，边界可能包含少量预热尾部，口径与延迟cohort不同
- 所有消费者核对run ID、正文SHA-256、全局消息ID，跨消费者也检测重复；对账全部阶段的尝试、Accepted、唯一接收、丢失/多余ID。异常不丢弃，标记失败并保留证据
- `backlog.json`由逐条ID/时间戳重建每秒应用未接收、已确认未接收数，处理消费早于发布确认的时序；**不等于代理内部ready/unacked时序**。代理真实深度在测量前后另行查询，最终必须排空。运行中应用未接收数超过10,000会停止发送并报告，不继续加压
- 合成数据库仍是单连接每5ms更新一行库存、读取一段汇总，保存每次事务服务延迟、调度延后、实际事务率。不是实际ERP，服务延迟也不包含全部排队等待；跟不上数据库到达率时同样要报告

### 原始证据、目标与限制

每次输出环境、每单元`summary.json`、每客户端`*.jsonl.gz`、`resources.jsonl.gz`、ID对账、`backlog.json`、前后容器/数据库/队列快照、普通服务器日志、`summary.csv`和SHA-256`MANIFEST.json`。WAL LSN增长和数据库大小增量涵盖整个单元（含预热/排空），不是物理SSD写入量。完整比较也对基础设施/setup/零样本失败立即停止，在`matrix-status.json`列出已记录和未运行单元；保留出错单元的全部已有证据。真实有数据的未达目标速率、≤10%候选目标false结果不被改成通过或删掉，继续比较。摘要另有实际观察时长、窗口是否完整、资源样本数与失败类别。job超时仍通过`if: always()`上传已有完整/部分数据。证书、私钥、数据库目录不上传。

GitHub artifact保留**30天**；重要实验应在到期前归档完整证据，不能只保存最快数字。数据库p95增幅相对同轮基线计算，`provisional_db_p95_le_10_percent`如实输出true/false/null。工作流绿灯表示普通流量/收集/对账成功，**不代表≤10%候选性能目标通过**。必须同时看目标字段、实际到达率、重复/失败、积压、基线噪声、原始样本和时窗；不通过降低durability、改变基线或丢弃不利单元粉饰结果。

复现（仅新的合成环境；Docker是CI预算约束工具，用户安装产品不需要Docker）：

```bash
set -euo pipefail
python -m pip install -r bench/requirements.txt
python -m pytest -q bench/test_measurement.py
docker build --pull -f bench/Dockerfile -t echoo-benchmark:local .
python bench/compare_ci.py --image echoo-benchmark:local \
  --output /tmp/echoo-budget-smoke-new --smoke
python bench/compare_ci.py --image echoo-benchmark:local \
  --output /tmp/echoo-budget-run-new \
  --repetitions 3 --warmup-seconds 30 --duration-seconds 120 --rates 200 400
```

主验收仍是**Win11 x64、真实4核/8GB/SSD、原生PostgreSQL18**，尚未执行。Linux两核服务器预算/四核总runner、Windows Server兼容CI都不能替代。容量、checkpoint/vacuum周期和长期稳定仍需后续经授权的代表性实验。

方法来源：[Docker资源约束](https://docs.docker.com/engine/containers/resource_constraints/)、[PostgreSQL官方源码安装](https://www.postgresql.org/docs/18/install-getsource.html)、[RabbitMQ确认与持久化语义](https://www.rabbitmq.com/docs/confirms)。这些描述不替代本项目的实际验证。

## 旧版探索工具与复现

- `bench/run.py`：向已经准备好的 AMQP 1.0 队列发送持久消息，逐条等待远端 Accepted；独立消费者手动确认，核对每条正文校验和、消息ID与重复数
- `bench/compare_local.py`：在新的隔离目录创建合成 PostgreSQL 数据库、临时证书、RabbitMQ 配置；交替测基线与两个代理。只支持 Linux 自动编排；原生 Windows 可使用相同 `run.py`，需另行准备并记录环境
- `bench/requirements.txt`：开发/测试依赖。它们不安装到 PostgreSQL 运行进程
- 每次运行保存概要 JSON 和逐消息原始延迟、ID、资源采样 JSON；失败也保留，不能只筛选最快的一次

新 `bench/run_duration.py` 支持独立多进程、预热和固定时窗；`compare_local.py --duration-seconds ... --warmup-seconds ... --producers ... --consumers ... --offered-rate ...` 可用作本机普通流量验证。这个本机路径**没有容器总资源预算**，不能冒充 clean-runner 对比。

示例（新空目录，不连接生产）：

```bash
python -m pip install -r bench/requirements.txt
python bench/compare_local.py \
  --pg-config /usr/lib/postgresql/18/bin/pg_config \
  --rabbitmq-server /usr/lib/rabbitmq/bin/rabbitmq-server \
  --rabbitmq-version <实际版本> \
  --work-dir /tmp/echoo-bench-独立新目录 \
  --output bench/results/本次实验 \
  --repetitions 3 --messages 5000 --baseline-seconds 10
```

需要预先安装对应 PostgreSQL 主版本的扩展和 RabbitMQ/Erlang；脚本不会替用户安装系统服务。生成的证书、数据库、Erlang cookie 仅属于这个隔离实验，禁止用于真实部署，也不应提交到仓库。脚本拒绝覆盖已有工作目录。

## 旧版默认负载与可比性

1. 相同机器、相同客户端、相同 1024 字节二进制正文、AMQP 1.0、mTLS、1 个发布连接、1 个消费连接、credit=32
2. 每条消息 durable=true，发布者等待 Accepted，消费者手动确认；echoo 使用普通 WAL 表、同步提交；RabbitMQ 使用单节点持久 classic 队列。不是 quorum/复制对比，也不是不同可靠性配置下的吞吐竞赛
3. 两个方案都同时运行 PostgreSQL 合成库存负载；基线只运行同一 PostgreSQL 负载。RabbitMQ 资源包含额外代理进程，echoo 资源包含 PostgreSQL 与原生 worker
4. 合成业务事务更新一行库存并查询一段库存汇总，目标间隔 5ms。这只是可重复的数据库干扰探针，不代表真实 ERP 事务
5. 每轮先测基线，两个代理交替顺序，以减少固定顺序偏差。初版是逐条同步确认模型，不代表批量/异步流水线最大容量
6. RabbitMQ 地址为 `/queues/bench`，echoo 地址为 `bench`；AMQP hostname/vhost 与 TLS SNI 分开设置，保持服务器名称校验开启

当前脚本记录确认吞吐、完成吞吐、发布与端到端 p50/p95/p99、数据库事务延迟、校验失败、重复和进程资源样本。RSS 简单求和会重复计算共享页，CPU 采样也不是隔离容器计费，不能过度解释这些资源数字。物理磁盘写入量、WAL 增长、稳定积压、长时间 vacuum 与24小时泄漏曲线仍需专门资格实验。

## 历史探索性冒烟结果（不是正式比较或容量结论）

已保存首轮探索性实测：[`bench/results/linux-pg18-2026-09-30/`](../bench/results/linux-pg18-2026-09-30/)，源代码提交 `d24d57ac4a04edd1be05d99b27c5034eee44473a`。概要见 `summary.csv`；每轮摘要JSON与原始JSON.gz完整保留，MANIFEST记录SHA256。

环境：共享Linux云容器，9个可见逻辑CPU、约9.73GiB可见内存、客户端同机；实际CPU/RAM cgroup额度未暴露，也未做绑核或独占资源隔离；PostgreSQL18.6、Proton0.40.0、RabbitMQ4.0.5，单节点持久classic队列，原生worker空队列轮询2ms。没有专用SSD型号或物理写入路径资格证据，不能视为Win11目标硬件成绩。

三轮各5,000条，每轮仅约8–11秒，每个代理共15,000条，所有发布Accepted及消费校验通过、观察到0重复/0错误。每轮发布确认吞吐如下：

| 代理 | 第1轮 msg/s | 第2轮 msg/s | 第3轮 msg/s | 三轮中位数 |
|---|---:|---:|---:|---:|
| echoo | 502.8 | 543.7 | 584.9 | 543.7 |
| RabbitMQ | 602.6 | 470.3 | 525.1 | 525.1 |

两组范围明显重叠，不能据约3.5%的中位数差宣称echoo更快。端到端p95的三轮中位数：echoo **5.18ms**，RabbitMQ **3.25ms**；这是明确需要优化的差距。

合成数据库事务p95的三轮中位数：单独PG **0.493ms**，与echoo混跑 **1.587ms**，与RabbitMQ混跑 **1.932ms**。相对基线分别约+222%/+292%；当前压力模型**不满足候选的p95增幅≤10%目标**。绝对延迟仍处毫秒级也不能代替目标达成；更不能推断真实ERP表现。接下来需用业务代表性到达率、事务结构与专用硬件校准，保留失败目标而不改数字粉饰。

这些是探索性共享环境结果，尚未覆盖容量上限、长稳、物理磁盘写入和门店硬件。开发期100条冒烟数据不计入上述探索性三轮样本。

## 候选目标与回归

- 门店容量目标：真实业务观测峰值的2倍下积压不持续增长；当前未取得实际峰值，不能先写任意 TPS 承诺
- ERP 干扰目标：代表性负载下 ERP p95 增幅不超过10%；实际 ERP 联调安排在独立产品完成后，目前未验证
- 后续固定硬件重复测量，若吞吐下降超过10%或 p99 上升超过20%，先与测量噪声比较，再阻断或复核；不能拿不同机器/不同确认模式直接判回归
- Win11 x64、4核、8GB、SSD、原生 PostgreSQL18 是主验收环境；Linux云端和WindowsServer CI属于不同证据层

## 必须关注的设计代价

全局容量计数使用单行事务锁，保证多个队列不会超额。SQL 入队和 ERP 同事务时，这个锁会持有到外层事务结束，可能让其他队列 AMQP 写入/ACK 达到锁超时。不要为了跑分削弱容量约束或持久化；缩短业务事务、记录跨队列长事务竞争，再决定是否需要更细粒度配额设计。

发布前仍需要：慢消费者、离线积压追赶、断连重投、磁盘不足、长事务竞争、重复积压/清理、原生Windows长期混跑。SIGKILL或immediate restart不是实际断电；存储硬件的易失缓存、文件系统与电源条件必须单独验证。

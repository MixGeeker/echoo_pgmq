# 性能验证、对比与持续目标

本页区分实测、候选目标与尚未验收事项。脚本不会关闭 `fsync`、`full_page_writes` 或同步提交来提高成绩。任何短时云端结果都不能作为门店 Win11 性能承诺。

## 工具与复现

- `bench/run.py`：向已经准备好的 AMQP 1.0 队列发送持久消息，逐条等待远端 Accepted；独立消费者手动确认，核对每条正文校验和、消息ID与重复数
- `bench/compare_local.py`：在新的隔离目录创建合成 PostgreSQL 数据库、临时证书、RabbitMQ 配置；交替测基线与两个代理。只支持 Linux 自动编排；原生 Windows 可使用相同 `run.py`，需另行准备并记录环境
- `bench/requirements.txt`：开发/测试依赖。它们不安装到 PostgreSQL 运行进程
- 每次运行保存概要 JSON 和逐消息原始延迟、ID、资源采样 JSON；失败也保留，不能只筛选最快的一次

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

## 默认负载与可比性

1. 相同机器、相同客户端、相同 1024 字节二进制正文、AMQP 1.0、mTLS、1 个发布连接、1 个消费连接、credit=32
2. 每条消息 durable=true，发布者等待 Accepted，消费者手动确认；echoo 使用普通 WAL 表、同步提交；RabbitMQ 使用单节点持久 classic 队列。不是 quorum/复制对比，也不是不同可靠性配置下的吞吐竞赛
3. 两个方案都同时运行 PostgreSQL 合成库存负载；基线只运行同一 PostgreSQL 负载。RabbitMQ 资源包含额外代理进程，echoo 资源包含 PostgreSQL 与原生 worker
4. 合成业务事务更新一行库存并查询一段库存汇总，目标间隔 5ms。这只是可重复的数据库干扰探针，不代表真实 ERP 事务
5. 每轮先测基线，两个代理交替顺序，以减少固定顺序偏差。初版是逐条同步确认模型，不代表批量/异步流水线最大容量
6. RabbitMQ 地址为 `/queues/bench`，echoo 地址为 `bench`；AMQP hostname/vhost 与 TLS SNI 分开设置，保持服务器名称校验开启

当前脚本记录确认吞吐、完成吞吐、发布与端到端 p50/p95/p99、数据库事务延迟、校验失败、重复和进程资源样本。RSS 简单求和会重复计算共享页，CPU 采样也不是隔离容器计费，不能过度解释这些资源数字。物理磁盘写入量、WAL 增长、稳定积压、长时间 vacuum 与24小时泄漏曲线仍需专门资格实验。

## 结果状态

正式对比结果尚在运行/复验；最终数据将放入 `bench/results/` 并列出源提交、版本、机器、durability 设置和所有失败。开发过程中完成的 100 条消息冒烟只证明脚本与真实代理能收发，不作为正式性能成绩。

## 候选目标与回归

- 门店容量目标：真实业务观测峰值的2倍下积压不持续增长；当前未取得实际峰值，不能先写任意 TPS 承诺
- ERP 干扰目标：代表性负载下 ERP p95 增幅不超过10%；实际 ERP 联调安排在独立产品完成后，目前未验证
- 后续固定硬件重复测量，若吞吐下降超过10%或 p99 上升超过20%，先与测量噪声比较，再阻断或复核；不能拿不同机器/不同确认模式直接判回归
- Win11 x64、4核、8GB、SSD、原生 PostgreSQL18 是主验收环境；Linux云端和WindowsServer CI属于不同证据层

## 必须关注的设计代价

全局容量计数使用单行事务锁，保证多个队列不会超额。SQL 入队和 ERP 同事务时，这个锁会持有到外层事务结束，可能让其他队列 AMQP 写入/ACK 达到锁超时。不要为了跑分削弱容量约束或持久化；缩短业务事务、记录跨队列长事务竞争，再决定是否需要更细粒度配额设计。

发布前仍需要：慢消费者、离线积压追赶、断连重投、磁盘不足、长事务竞争、重复积压/清理、原生Windows长期混跑。SIGKILL或immediate restart不是实际断电；存储硬件的易失缓存、文件系统与电源条件必须单独验证。

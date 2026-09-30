# 容量预留：SQL机制收益未转化为稳定原生收益

2026-09-30，结论保持实验候选，不将0.1.2推广进已交付的PG18预览20260930.1。三对同机普通native固定负载都通过原稳定性门槛，但CPU与尾延迟没有一致改善；不能把SQL批量调用的收益直接当作MQ产品收益。

## 同机原生比较

A为七计划基线aa3f21d、SQL0.1.1，B为本地d909109、SQL0.1.2（首次公开6660cbb同tree）。C/headers/历史SQL逐字节相同，共用已安装native库64f85344dac954bc88a9f7d9431d546a547529d06888b1b5a115a8557731aa26。所有20份前后catalog快照都核验了真实版本、函数签名、原始prosrc与SHA；B原始函数SHA为6d294ce35e68505d92ff559f6fad48d4304b55da22ccf63f5a4887f01efa6d08。构建树库910b296…与安装库不同，仅ELF .dynstr内容差异，与RPATH安装改写一致，未把不同哈希假装相同文件。

固定seed20260930，顺序G0、P1-B/A、G1、P2-B/A、G2、P3-A/B、G3。每cell暖机5秒、测量30秒，PG-only gate暖机5秒、测量20秒；一次349.313秒正常完成，无重试。1生产者/1消费者、1024B合成payload、credit32、publisher inflight1，目标200msg/s并行合成DB200TPS。它是固定负载点，不是开放到达容量上限。

| B相对A | P1（BA） | P2（BA） | P3（AB） |
|---|---:|---:|---:|
| 生产者确认p99（ms） | 2.003→2.272，+0.269 / +13.42% | 1.875→2.091，+0.216 / +11.53% | 2.575→2.248，−0.327 / −12.71% |
| 消费收件E2E p99（ms） | 3.693→4.108，+0.414 / +11.22% | 3.564→3.751，+0.187 / +5.26% | 4.110→3.638，−0.472 / −11.48% |
| 合成DB p99（ms） | 1.447→2.028，+0.581 / +40.18% | 1.600→1.867，+0.267 / +16.69% | 1.682→1.907，+0.225 / +13.37% |
| worker CPU/实际收件（ms） | 0.953586→1.035762，+8.62% | 0.943460→0.977913，+3.65% | 0.974333→0.966925，−0.76% |
| 12个持续自有PID CPU/收件 | +8.50% | +3.72% | −1.06% |

这些是三对短实验的描述结果，不是显著性或因果证明。没有剔除较差结果，也不因第三对/最大值有利而宣布整体优化成功。PG-only四个gate全窗和半窗完成速率均≥190TPS，相邻吞吐漂移≤5%、双侧p99漂移≤max(1ms,前次p99×50%)；没有放宽门槛。该环境门槛并不证明真实ERP的10%暂定目标通过。

## 对账、采样和环境边界

41,975条全阶段生产者ID、远端Accepted记录与消费收件ID精确一致，测量发送cohort35,975条，无记录中的缺失、重复、额外、Rejected或checksum错误；六个cell实际完成199.70–199.93msg/s，DB199.83–199.97TPS。20份边界队列深度均为零。粗粒度1秒样本的confirmed backlog峰值0–1，完整事件流重建的峰值为2，P3-A为3；不能用采样峰值代替精确瞬时峰值。

消费原始行的时间戳在receiver.accept()之前；最终delivered计数支持每条消息已调用本地ACCEPTED，但没有记录远端消费ACK commit的逐条ID或时间。收件E2E不能改名成ACK延迟。payload原字节未保存在性能附件，独立复算使用已记录checksum有效标志与ID连接，不声称重新计算网络payload哈希。

CPU使用每cell60个样本内相同PID+创建时间配对；实际首尾跨度29.64–29.66秒，分母由该跨度原始收件重新计算并与首尾counter精确一致。两个内部样本存在±1的顺序采样/accept时点差异，保留披露，不影响本次端点分母。12 PID求和含合成DB及观察相关PG工作，不是整个cgroup资源；采样间出现又退出的进程与未覆盖边缘没有完整CPU上界。RSS求和可能重复共享页。

Linux云workspace overlay为fsync=volatile，/tmp为tmpfs；服务仅绑CPU0/1、客户端与观察者2/3，未获得可执行的cgroup CPU/内存/swap总预算。此证据不能用于持久SSD、Win11四核8GB、真实ERP或物理断电结论。保持fsync/full_page_writes/synchronous_commit开启，未弱化Accepted语义。

整段PG WAL LSN在B稍低，但窗口含暖机、合成DB、排空与观察开销；pg_stat_io没有自定义AMQP listener backend行，因此其写入/fsync计数不是原生worker或全服务的完整I/O测量。不能仅用较小WAL窗口解释CPU/延迟。

## 与早期SQL机制数据的关系

普通独立原型36项SQL检查通过。单次正常自动提交publish的无FPI嵌套SQL WAL，从7记录/1612B到6记录/1558B，−54B/−3.35%；队列预留提前更新引入外键KEY SHARE WAL，不能按少两把锁推算双倍节省。队列满时捕获拒绝的特定SQL夹具从142B到168B（+26B）；含夹具子事务，不是长期拒绝负载资格。

另一批次SQL实验每臂5000调用共用一个外层提交，backend CPU/调用三对−21.79%/−24.39%/−29.52%；该长事务的计数链、缓存和提交摊销与native逐条操作不同。批次WAL约少107.99B/调用，也不同于自动提交54B。两组结果都保留，native结果没有复现稳定净收益，因此不以SQL微基准作为采用理由。

## 普通跨平台正确性证据

[CI36749616970](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36749616970)，head67df046ae29b59c648c15b94a0af687e00172419，实际checkout e60b8f25258360cb0831bfc4e9565e27aa75e78f，与head同tree4ce9c6aacf34fae6ba4261216a8be3b34887b8ad：Linux/Windows Server2022×PG18/17/16，源码安装与候选重装12轮，共704项普通测试，0失败/错误/跳过，12个core.sql完成标记。PG18每轮64、PG16/17每轮56。12外层artifact、6内层候选及逐文件manifest全部哈希核验；12轮真实SQL版本/函数SHA一致、fast stop=0、status=3、PID消失。[机器可读核验](evidence/sql-reservation-0.1.2-ci704.json)。

前一次CI36747027655只有4组合通过408项，Windows16/17在实际函数正文身份比较前失败。原因定位为这些版本保留SQL文件CRLF，而读取源文件时通用换行转换隐藏了差异。现用.gitattributes固定SQL为LF，读取精确UTF8并拒绝CRLF，比较仍严格；新增9项正常身份/检出测试。不是忽略空白或削弱断言来通过。

本地先前源安装55项和原归档实际重装55项均通过，首轮测试夹具2失败的历史保留。独立普通集成审查无阻断发现，完整人工安全/解析器/故障资格继续暂停；所有manifest保留blocked_security_review。正确性普通CI绿色不等于性能采用或生产资格，0.1.2不进入preview20260930.1，也未合并main。

## 原始证据索引

本地冻结原始目录reservation-native-mechanism/adapter/result-one有233项文件，MANIFEST SHA256为e2bfe2024251d2ae67ce4183c306640be6c81797350e1869f43936380d4d0a1b。独立audit_reservation_native.py重算ID/时钟/量化分位数、CPU、20份catalog与源码来源，413,949项算术/一致性断言通过；这个数不是独立样本数。完整JSON保留所有quantile、绝对差、PID边界、PG统计及不利观测，原始文件不含PGDATA或私钥。正常清理记录全部成功。

# 原生发布唤醒：本地机制实验

## 结论与代码边界

基线为96c18db（原生/SQL与51ca717相同）。本次只保留`src/echoo_pgmq.c`中提交后唤醒：`echoo_db_publish`已成功提交后，将**匹配队列且当前可领取**的sender标为立即轮询；若消费者在本轮遍历中先于生产者处理，用一次性标记保证下一轮不再睡眠。原有credit、inflight、输出/全局缓冲与关闭状态检查仍在领取处执行；SQL入队继续依赖原有周期轮询。

默认50ms和固定预算实验2ms参数均未修改；SQL、ACL、容量、WAL/同步提交、Accepted与ACK持久化顺序、候选10%门槛都不变。没有重写队列公平性、单消费者通知或批量提交。另做的deadline夹紧/组合方案没有一致独立收益，已撤回，不混入本补丁。

**这是消息延迟与CPU的权衡，不是CPU优化结论，也没有证明ERP候选目标已达成。** 新候选的六平台普通CI及同预算云端复测尚待独立结果；上一份已核实兼容候选仍为a501852/9068d10。

## 本地条件与全部结果

- Linux云端、PG18.6、Proton0.40.0、RelWithDebInfo、test hooks OFF；fsync/full_page_writes/synchronous_commit保持开启
- 服务端绑CPU0–1、客户端2–3；**没有4GiB/0swap cgroup额度，数据位于tmpfs**。不是磁盘耐久延迟、Win11或ERP验收，也不能与GitHub固定预算表直接拼接
- 2秒暖机；首次探索8秒测量，配对复测12秒；1×1/4×4、200/400条每秒，1024字节、mTLS/EXTERNAL、逐条远端Accepted与手动ACK，保留实际到达率和不利波动
- 共30单元全部正常对账/排空，含各阶段93,442条Accepted与唯一接收一致，无重复、缺失或发布/校验错误。被舍弃变体与抖动单元也保留
- 函数调用次数包含setup/暖机/排空；延迟和进程CPU取测量窗口，CPU不是cgroup计费指标

[全30单元JSON](../bench/results/native-wake-local-20260930/summary.json)与[CSV](../bench/results/native-wake-local-20260930/summary.csv)保留baseline、wake、deadline、combined四种方案，不筛选最快结果。

### 最重要的配对观察

| 配置 | 基线E2E p95 ms | 唤醒E2E p95 ms | 基线worker核数 | 唤醒worker核数 |
|---|---|---|---|---|
| 2ms / 4×4 / 400，第1对 | 5.271 | 1.763 | 0.442 | 0.491 |
| 2ms / 4×4 / 400，第2对 | 5.745 | 4.894 | 0.424 | 0.468 |
| 默认50ms / 4×4 / 200，第1对 | 51.792 | 1.555 | 0.150 | 0.223 |
| 默认50ms / 4×4 / 200，第2对 | 51.778 | 3.446 | 0.152 | 0.239 |

2ms/4×4/400的CPU增加约10–11%，绝对约0.045–0.049核。默认50ms的大尾延迟改善伴随明显空查询与CPU增加；该配置每消息空claim约0.42→4.06。2ms各配对单元消息p95均降低，但共享主机噪声很大，不能作稳定倍数承诺。比如第二个400单元的合成DB p95由4.321升至4.757ms，不能只展示改善的消息延迟。

## 普通正确性验证

所选原生库SHA256：`84c669318bd26c8de9a770c84e0938ec1bf805945fa4d702bd8fe055c11a49e4`；最终注释调整后重编译的库与被测库一致。

- Linux PG18：40项既有普通回归（含rhea8项）及core.sql完成标记通过，未修改40/32清单或计数
- 额外5项独立普通检查：两种连接遍历顺序；零credit队列保持ready且不被其它队列投递影响，补credit后恢复；SQL入队周期回退；4消费者全局消息ID唯一且ACK后排空。没有要求每消费者恰分到一条或规定严格公平性
- 检查仅使用正常完成超时防死锁，没有易受CI调度影响的毫秒性能通过线。早期测试脚本的借用Message正文生命周期及不合理分配假设错误已单独保留，产品没有为迎合错误断言而修改

复现独立检查（**Linux/PG18/Proton OpenSSL专用**，非Windows SChannel或PG16资格）：

```sh
python scripts/check_normal_wake.py /tmp/new-normal-wake \
  --pg-config /path/to/candidate/bin/pg_config --repo /path/to/echoo_pgmq
```

须先安装对应hooks-OFF候选、开发Python依赖和可信库搜索路径。工作目录必须不存在；脚本在访问cluster前拒绝任何已有目录，避免改写现有数据库。它只建合成loopback集群和一次性测试证书，并正常停止；状态目录含私钥，禁止上传。普通CI计数不包含这5项独立检查。

## 原始证据与下一步

原始导出`native-wake-ab-evidence.tar.gz`，SHA256：`95a20d1b6cf607d60d673fadbf4b252b906f67779285d93231b1ca9fd154016a`；[原始逐文件清单](../bench/results/native-wake-local-20260930/SOURCE-MANIFEST.json)587项已核对。导出只含合成计数、日志、源码变体与复现脚本，不含证书、私钥、PGDATA或二进制。完整导出另行保留，仓库只存紧凑摘要。

新固定预算运行使用独立分支`benchmarks/wake-pg18-20260930`；原始`benchmarks/controlled-pg18-20260930`仍固定51ca717。测量脚本、2CPU/4GiB/0swap、2ms轮询、三轮30秒暖机+120秒测量、到达率、持久性与目标保持一致。记录新固定提交与终态证据后再判断是否保留该权衡，不能把本地tmpfs结果当云端复测已通过。

安全/解析器/新fuzz及故障资格仍暂停；本次没有恢复这些活动。

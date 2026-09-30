# 提交后唤醒试验数据：2026-09-30

本目录为固定提交472e62698137d0e1035d04775d443f1cbcc1ae33、[运行36672765081](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36672765081)的紧凑证据。[完整中文解读](../../../docs/benchmark-wake-results.md)优先说明基线不稳与撤回决定。

- 27/27单元，全部原始摘要由两份只读脚本分别复算
- 原始ZIP及728文件散列、文件集合完整性全部通过
- 1,000,146条全阶段尝试/Accepted/唯一接收完全对账，无观察到的消息错误
- 24个代理单元均未达原有DB p95增幅≤10%目标；echoo11/12低于设定到达率95%，保留所有慢轮次及未达目标
- PG-only自身实际吞吐降低、p99长尾变大；这是首要混杂，不能把跨VM差异直接归因于wake
- 实际完成工作量不同，不能从较低CPU/写入量声称效率提高
- 独立smoke步骤报告成功，但其单独原始附件传输失败，未完成本地核验；5秒smoke未混入正式测量

本目录不包含体积较大的逐客户端原始流；完整原始artifact见provenance.json，GitHub保留至2026-10-30 06:33:32 UTC。完整数据已另行归档；仅有manifest不能重建原始流。任何导出均不含私钥或数据库目录。

## 复现只读核验

将完整artifact下载并解包至独立目录，执行：

```sh
python audit_raw.py /path/to/extracted > independent.json
python verify_raw_second.py /path/to/extracted --expected-sha 472e62698137d0e1035d04775d443f1cbcc1ae33 > second.json
```

两份脚本只读取已有数据，不连接broker或发起负载。SOURCE-MANIFEST.json对应完整原包；SUMMARY-SHA256.json覆盖本目录紧凑文件。数据收集成功不等于容量、安全、fuzz或故障资格通过。

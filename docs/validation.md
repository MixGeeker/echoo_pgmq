# 普通兼容验证记录

**当前状态（06:39 UTC）：472e626唤醒试验虽通过下面的普通兼容测试，但固定预算复测表现不支持保留；原生代码现恢复96c18db/a501852，撤回提交普通CI待核实。下面472e626产物仅保留为试验历史，不是推荐的新候选。此前参考证据见[96c18db版本验证记录](https://github.com/MixGeeker/echoo_pgmq/blob/96c18dbd2a078f941e5d153b32bbcd19cea2f186/docs/validation.md)。**

## 当前已核实候选：提交后唤醒与独立客户端

- PR头提交：`472e62698137d0e1035d04775d443f1cbcc1ae33`
- 实际checkout与候选MANIFEST提交：`f1fa034b90a681ee0a750c20d8c57401d37602be`，为GitHub对PR#1生成的测试合并提交；没有合并到main
- 两者Git tree均为`d3f18149c8c515ea31c94fdab48d5e147aea0579`，已用GitHub提交与compare API核实文件完全相同。候选manifest保留真实checkout身份，不改写为PR头
- [普通CI运行](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36672854753)：Ubuntu24.04 Linux与原生Windows Server2022，每个平台PG18、17、16；PG18为主目标

六个组合均执行两轮：第一次从构建结果安装，第二次从候选归档校验并重新安装。第二轮移除构建用Proton/OpenSSL库路径，使用归档和PostgreSQL发行版自身依赖。

| 平台/版本 | 源码安装 | 归档重装 | 独立客户端 |
|---|---:|---:|---|
| Linux PG18.6 | 40 | 40 | 每轮8项rhea |
| 原生Windows PG18.6 | 40 | 40 | 每轮8项rhea |
| Linux PG17.11、PG16.15 | 各32 | 各32 | 不追加rhea |
| 原生Windows PG17.11、PG16.15 | 各32 | 各32 | 不追加rhea |

已下载并逐份核对：合计416项、0失败、0错误、0跳过；12份环境记录均明确`sql_scripts_passed=["core.sql"]`、`ordinary`范围与`process_crash_tests_enabled=false`。12个GitHub外层artifact SHA256、6个候选内层ZIP旁文件SHA256，以及6份候选MANIFEST中的每文件散列均吻合。此结论不是只根据job绿色状态。

PG18实际客户端为Node.js **24.21.0**、rhea **3.0.5**（debug4.4.3/ms2.1.3）。四轮各8项、各16条JSONL，记录双向TLS验证成功、实际TLS1.3、direct AMQP/ANONYMOUS/EXTERNAL，以及默认FIRST/手动SECOND、完整二进制metadata、释放重投与正常关闭后新连接重投。锁文件在Linux为LF、Windows为checkout转换的CRLF，两种实际SHA256均保留并核实只有换行差异。没有关闭CA或服务器名验证；这些记录不等于强制TLS1.2/1.3完整版本矩阵、自动网络恢复或任意AMQP客户端认证。

## PG18候选与证据

- [原生Windows PG18候选](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36672854753/artifacts/11078872972)
- [原生Windows PG18证据](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36672854753/artifacts/11078663710)
- [Linux PG18候选](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36672854753/artifacts/11078427506)
- [Linux PG18证据](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36672854753/artifacts/11078617366)

全部12份归档的ID、GitHub外层SHA256、大小、到期时间，以及逐轮计数/客户端参数见[机器可读证据索引](evidence/ordinary-472e626-artifacts.json)。GitHub当前保留至2026-10-14；它们不是永久正式Release。下载Actions artifact可能需要登录拥有访问权的GitHub账号。

Windows候选使用归档Proton DLL和PostgreSQL发行版提供的OpenSSL及标准Windows/VC运行库；服务没有Python/Node运行时依赖。Windows Server结果不能替代Win11门店硬件、服务身份与真实SSD验收。

本次候选包含提交后唤醒匹配消费者的小改动；本页只证明普通兼容，性能代价与独立实验见[机制报告](worker-performance.md)。固定预算复测尚未完成。

## 保留的上一版普通证据

`a501852`及相同树的测试合并`9068d10`在[此前普通CI](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36668952734)也完成416项及全部归档散列核对；[该次证据索引](evidence/ordinary-a501852-rhea-artifacts.json)保留，代表唤醒改动之前的兼容状态。


`152a73c35ac9d2ae85e09686f33c81ba870a0d84`的[普通CI](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36662296554)完成六组合两轮各32项与core.sql；原有[12份归档索引](evidence/ordinary-152a73c-artifacts.json)继续保留。这是新增独立rhea验证前的证据，不能用其32项计数描述当前PG18的40项。

## 修复过的兼容与测试问题

1. Proton0.40 Windows OpenSSL代码的MSVC pragma和InitOnce回调兼容问题：固定原始源码散列后应用最小补丁，保留Apache许可与修改通知
2. 私有Python暂存目录整体move保留了不适合PostgreSQL受限子进程的ACL：改为校验后复制到正常继承的隔离安装目录，没有关闭受限令牌、修改系统ACL或要求完整Windows SDK
3. SChannel测试客户端空密码P12用空字符串引发参数错误：按API约定使用None，保持CA及服务器名验证
4. Windows ZIP名称归一化影响归档校验：在归一化前检查原始名称，保持原有拒绝断言
5. Windows PG16的psql位置参数导致core.sql未执行但进程exit0：改为显式--dbname及正确选项构造，拒绝额外参数警告，必须出现脚本完成标记；增加4项harness回归
6. 添加rhea后的31765e8 Windows PG18运行在CLI预检失败，未执行测试；a501852改为完整PowerShell string[]参数数组，两轮均记录精确参数后执行，再用JUnit/core标记验证覆盖

普通套件覆盖正常AMQP收发/提交/重投语义、SQL事务与配额并发、备份恢复、扩展版本迁移，以及归档安装和harness本身。

## 尚未通过的发布闸门

所有候选manifest仍为 `qualification_status=blocked_security_review`。人工安全审查不完整，存在尚未验证的解析资源上限静态候选；安全/完整故障资格工作流保留但当前不触发。此前自动检查或进程恢复测试的通过，不代替最终安全资格。

尚未完成：实际Win11 x64四核/8GB/SSD、服务身份与门店环境、物理断电/磁盘故障、长期积压与vacuum、真实ERP、项目许可证及正式签名发布。当前归档安装器仅适用于显式指定的可丢弃测试安装，不是生产升级工具。禁止把这份普通验证记录理解为完整产品可生产发布。

## 基准来源

固定预算首轮e2f71c9因采样器实现问题产生零测量样本，属于基础设施失败，不作为性能结论。修复后的实验固定提交为 `51ca71797859a5d09d16f573dde71795176fe240`；与普通候选152a73c相比，原生代码、SQL、CMake/控制文件、依赖下载脚本和Dockerfile没有差异，变化在测量流程和文档。基准应按自己的固定提交报告，不能混写成候选提交实测。

[真实Docker smoke证据](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36663717022/artifacts/11075865839)已验证3单元完整时窗、实际HTTP资源样本与2CPU/4GiB/0swap预算。echoo/Rabbit各700条含暖机、无重复/缺失/发布失败、最终队列0。smoke只证明基础设施和计数路径；同次运行的27单元完整测量现已完成并从原始记录复算，见[固定预算实测报告](benchmark-results.md)。完整artifact为11077458716；没有把基准SHA改写成普通候选SHA，也没有以数据采集成功替代性能目标达成。详见[性能方法与边界](benchmark.md)。

唤醒候选的另一次固定预算复测锁定`472e62698137d0e1035d04775d443f1cbcc1ae33`，[运行36672765081](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36672765081)仍在进行。旧51ca717基准、原始数据及其失败目标保留，不能把两次代码状态或不同runner混成同一次实测。

# 普通兼容验证记录

## 当前整合候选与证据来源（2026-10-01）

本次将PR4的隔离预览包装器、PR2的七项SPI计划复用与PR1的原生扩展整合；不包含PR3的SQL0.1.2实验。默认扩展版本仍为0.1.0，附0.1.1加法迁移。只有最终整合树的普通CI核实通过后才合入main；执行状态和精确head见[PR1检查记录](https://github.com/MixGeeker/echoo_pgmq/pull/1/checks)。合并代码不解除blocked_security_review，也不宣布生产就绪。

已有证据必须分别引用：

- 固定450681d二进制：[CI36720731289](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36720731289)，实际checkout191d994与450同tree d0abc20，六组合源码/归档两轮共464次普通执行；[逐包索引](evidence/spi-plan-ci450681d.json)。已交付.2仍携带这批原始二进制
- 后续包含预览文件的重建：[CI36776023129](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36776023129)，head fd30a1f，实际checkout ad589825/tree8d8832，另一次464次普通执行；[逐包索引](evidence/preview2-ordinary-36776023129.json)。这批新归档没有替换已发.2
- .2包装器：[Windows CI36776023123](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36776023123)和Linux PG18.6本地七阶段普通烟测；[完整版本记录](preview-20260930.2/README.md)含12项离线检查、合法SQL→AMQP示例、实际编码限额与正常停止证据

两次464都为0失败/错误/跳过、12个core.sql完成标记；每个平台PG18每轮44项，PG16/17每轮36项。它们是不同固定提交的历史证据，不能相加冒充一次最终候选验证，也不能仅凭旧job绿色认定新整合head通过。Windows Server仍不等于Win11；普通回归仍不包含暂停的完整安全/故障资格。

## 历史基线：恢复唤醒试验前代码的37fc015

- PR头：`37fc0153e1cf84a5a0c1fd84caec06342270b1fb`
- 实际checkout与候选MANIFEST：`537d2f042a334516bd34872fe39895843f70af88`，GitHub为草稿PR#1创建的测试合并提交；没有合并到main
- 两者Git tree均为`2ba52d8d16cad797207fb2a2678e7659d4d0ea75`，compare文件差异为空；manifest保留实际checkout身份
- [普通CI36679556956](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36679556956)：Linux Ubuntu24.04/原生Windows Server2022 × PG18/17/16六组合
- 已独立核对src/include/SQL/CMake/control共9个远端blob与96c18db/a501852完全相同；26个普通测试/脚本blob与上一已验证提交一致，没有以删减用例取得通过

472e626唤醒实验因尚无稳定收益证据而保守撤回。新运行的PG-only基础负载自身不稳、共享runner不同，不能说已经证明代码导致退化。详细失败数据和可比性限制见[唤醒试验报告](benchmark-wake-results.md)。

每个组合两轮：源码构建安装，随后从校验后的候选归档重装，移除构建Proton/OpenSSL库路径，再执行同一普通清单。

| 平台/版本 | 源码安装 | 归档重装 | 独立客户端 |
|---|---:|---:|---|
| Linux PG18.6 | 40 | 40 | 每轮rhea8项 |
| 原生Windows PG18.6 | 40 | 40 | 每轮rhea8项 |
| Linux PG17.11、PG16.15 | 各32 | 各32 | 不追加rhea |
| 原生Windows PG17.11、PG16.15 | 各32 | 各32 | 不追加rhea |

合计**416项，0失败/错误/跳过**；12份JUnit和environment逐一核验，全部明确core.sql完成、ordinary范围与process_crash_tests_enabled=false。PG18四轮共32项rhea、64条JSONL，Node24.21.0/rhea3.0.5及传递依赖/锁文件完整性均通过；实际TLS1.3、direct/ANONYMOUS/EXTERNAL与FIRST/SECOND场景按[协议矩阵](protocol.md)记录。额外5项本地手动检查不计入416项。

12个GitHub外层artifact、6个内层候选ZIP旁文件和每份MANIFEST的完整文件SHA256均核对一致。源码/归档两轮均保留记录，不仅根据job绿色状态判断。

## 历史37fc015的PG18候选与证据

- [Windows PG18候选](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36679556956/artifacts/11080837919) · [Windows PG18证据](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36679556956/artifacts/11081805822)
- [Linux PG18候选](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36679556956/artifacts/11080927974) · [Linux PG18证据](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36679556956/artifacts/11080863418)
- [完整机器可读索引](evidence/ordinary-37fc015-artifacts.json)：全部ID、大小、到期、散列、逐轮计数/客户端参数和源码一致性

GitHub归档当前保留至2026-10-14，下载可能需登录有访问权的账号；不是永久正式Release。Windows Server不能替代Win11门店硬件/服务身份/SSD验收。服务运行不依赖Python或Node，OpenSSL由可信PG/系统发行版提供，归档仅捆绑Proton及扩展。

## 保留的历史证据

- [152a73c普通CI](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36662296554)：六组合两轮各32项，新增rhea之前；[索引](evidence/ordinary-152a73c-artifacts.json)
- [a501852/9068d10普通CI](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36668952734)：416项；[索引](evidence/ordinary-a501852-rhea-artifacts.json)
- [已撤回试验472e626/f1fa034普通CI](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36672854753)：416项；[索引](evidence/ordinary-472e626-artifacts.json)。这证明其普通兼容，不代表性能验收或目前仍推荐该试验包

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

## 固定负载证据与下一步

- [51ca717首次固定预算实测](benchmark-results.md)：27单元，echoo12/12、Rabbit10/12未达暂定合成DB p95增幅≤10%
- [472e626唤醒试验实测](benchmark-wake-results.md)：27单元，echoo明显未达到设定发送速率，24/24代理单元未达暂定DB目标；PG-only本身不稳定，跨共享runner不能作严格A/B
- 两轮全部原始数据、慢轮次与失败目标保留。普通CI通过和数据采集成功都不能替代性能、故障或安全验收
- [短机制诊断](ordinary-tail-results.md)已在8b333f1执行一次，G0失败后正常停止，未运行A/B；此后独立同机普通实验见[最新进度](PROGRESS.md)。旧失败和未跑单元保留，没有重跑失败runner刷绿

# 普通兼容验证记录

## 当前已知良好候选

- 被测提交：`152a73c35ac9d2ae85e09686f33c81ba870a0d84`
- [完整普通 CI 运行](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36662296554)
- 范围：Ubuntu Linux 与原生 Windows Server 2022；每个平台都覆盖 PG18、17、16
- PG18 为优先目标。Windows Server 结果不能替代 Win11 门店硬件验收

六个组合均执行两轮：第一次从构建结果安装，第二次从候选归档校验并重新安装。第二轮移除构建用 Proton/OpenSSL 库路径，使用归档和 PostgreSQL 发行版自身依赖。

已逐份下载核对全部证据：12 份 JUnit 每份32项、0失败、0错误、0跳过；12 份环境记录均包含 `sql_scripts_passed=["core.sql"]`，并明确 `ordinary` 范围和 `process_crash_tests_enabled=false`。这不是只根据job绿色状态得出的结论。

## PG18候选与证据

- [原生 Windows PG18 候选](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36662296554/artifacts/11074957155)
- [原生 Windows PG18 证据](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36662296554/artifacts/11075041845)
- [Linux PG18 候选](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36662296554/artifacts/11075200022)
- [Linux PG18 证据](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36662296554/artifacts/11075011882)

全部12份归档的ID、GitHub外层SHA256、大小、到期时间见 [归档索引](evidence/ordinary-152a73c-artifacts.json)。GitHub当前保留至2026-10-14；它们不是永久正式Release。候选内部另有每文件散列、依赖版本与构建信息。下载GitHub Actions artifact可能需要登录拥有访问权的GitHub账号。

Windows PG18候选已核对外层/内层校验和、全部manifest文件散列、Proton DLL与许可证/补丁通知。PE依赖闭包为PostgreSQL、Proton、PG发行版提供的OpenSSL3及标准Windows/VC运行库；没有Python服务运行时依赖。

## 修复过的兼容与测试问题

1. Proton0.40 Windows OpenSSL代码的MSVC pragma和InitOnce回调兼容问题：固定原始源码散列后应用最小补丁，保留Apache许可与修改通知
2. 私有Python暂存目录整体move保留了不适合PostgreSQL受限子进程的ACL：改为校验后复制到正常继承的隔离安装目录，没有关闭受限令牌、修改系统ACL或要求完整Windows SDK
3. SChannel测试客户端空密码P12用空字符串引发参数错误：按API约定使用None，保持CA及服务器名验证
4. Windows ZIP名称归一化影响归档校验：在归一化前检查原始名称，保持原有拒绝断言
5. Windows PG16的psql位置参数导致core.sql未执行但进程exit0：改为显式--dbname及正确选项构造，拒绝额外参数警告，必须出现脚本完成标记；增加4项harness回归

普通套件覆盖正常AMQP收发/提交/重投语义、SQL事务与配额并发、备份恢复、扩展版本迁移，以及归档安装和harness本身。

## 尚未通过的发布闸门

所有候选manifest仍为 `qualification_status=blocked_security_review`。人工安全审查不完整，存在尚未验证的解析资源上限静态候选；安全/完整故障资格工作流保留但当前不触发。此前自动检查或进程恢复测试的通过，不代替最终安全资格。

尚未完成：实际Win11 x64四核/8GB/SSD、服务身份与门店环境、物理断电/磁盘故障、长期积压与vacuum、真实ERP、项目许可证及正式签名发布。当前归档安装器仅适用于显式指定的可丢弃测试安装，不是生产升级工具。禁止把这份普通验证记录理解为完整产品可生产发布。

## 基准来源

固定预算首轮e2f71c9因采样器实现问题产生零测量样本，属于基础设施失败，不作为性能结论。修复后的实验固定提交为 `51ca71797859a5d09d16f573dde71795176fe240`；与普通候选152a73c相比，原生代码、SQL、CMake/控制文件、依赖下载脚本和Dockerfile没有差异，变化在测量流程和文档。基准应按自己的固定提交报告，不能混写成候选提交实测。

[真实Docker smoke证据](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36663717022/artifacts/11075865839)已验证3单元完整时窗、实际HTTP资源样本与2CPU/4GiB/0swap预算。echoo/Rabbit各700条含暖机、无重复/缺失/发布失败、最终队列0。smoke只证明基础设施和计数路径，27单元完整测量仍运行中。详见 [性能方法与边界](benchmark.md)。

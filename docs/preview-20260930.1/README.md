# PG18 preview-20260930.1：Windows 普通流程验证

此目录保留已交付 `guide/` 的原始字节，不改变预览标签、包装器或候选二进制。真实扩展及候选包版本仍为 **0.1.0**，没有 0.1.2。固定输入和全部 guide 文件的 SHA256 见 `fixed-inputs.json`。

新增工作流 `preview-smoke.yml` 在 Windows Server 2022、Python 3.12、Node.js 24 上直接运行这份包装器。它用自动、只读 `GITHUB_TOKEN` 下载原 artifact 11099301285，分别核对外层 ZIP、原候选 ZIP 和原 `.sha256`，再使用 guide 自带的下载器取得固定官方 PostgreSQL 18.6。首次安装时，新建的 `echoo-preview-…` 根目录仅包含 `pg/`。

依赖安装使用附带锁文件和 `npm ci --ignore-scripts`，由 `node.exe` 直接运行同一安装目录的 `npm-cli.js`，不经过批处理或 shell；OpenSSL 使用 runner 现有命令的绝对路径。运行顺序为 `install → init → start → sql-demo → amqp-demo → stop`，并要求直接执行的 `pg_ctl status` 退出码为 3。流程不注册服务，不使用现有 PostgreSQL 或数据目录。

证据仅允许上传 `result.json`、经过凭据脱敏的 `stages.log` 与 `postgres.log`、以及不含消息正文的 `demo-results.json`。结果记录源码/脚本/归档哈希、实际子进程退出码、Node/npm CLI 路径、SQL 演示完成标记、rhea 三项成功记录和正常关闭状态。`sql_demo_marker` 指 `SQL_ENQUEUE_READ_ACK_OK`，不是原 `tests/sql/core.sql` 的完成标记，不计入原 core SQL 回归次数。失败时仅在确认本次新建目录身份且 PostgreSQL 确实运行后执行普通 `pg_ctl -m fast stop`；保留本机故障材料，不上传 PGDATA、证书、密码或整个运行根目录。

## 已完成的 Windows 实跑（2026-09-30 17:53 UTC）

[普通 smoke36754291541](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36754291541)已成功，并已下载4文件原始证据独立核验。测试head为`c152c282e0a86c4d3261a0a2d5ae3e704a5c360c`，实际CI checkout为`1b0b0d8fb1a3942c25d0a05ac4cf23f54a9bec88`，二者tree均为`8f3cfb3500b9567c81f3c1df949d177860aa7183`。[逐项核验](windows-verification-36754291541.json)。

- 原候选内ZIP SHA保持`a8107211653c966dca2792726a84e4013bd7c1eb010e40700f755d3d425ff164`；12个guide文件与已经交付的套件逐字节一致，preview.py SHA仍为`f39a3a96eca34d432c589f01e504c69286e6dc284ebc80bd794b607b605149af`
- 独立PG18.6、Windows Server2022 image20260927.320.1、Python3.12.10、Node24.21.0/rhea3.0.5；所有6个preview阶段退出码为0，直接pg_ctl status为3，日志确认正常shutdown
- SQL事务入队/read/receipt ACK演示标记通过；rhea roundtrip/release/正常关闭重连，均核对生产者Accepted后的完整存储字节、metadata、SECOND手动ACK的远端settlement及最终空队列
- 原始证据ZIP SHA为`214b11307e9ecae9717c28c2150c5ba06dc171815c431adab6261a343b4f751f`，无私钥/密码/PGDATA

用户已经收到的preview20260930.1两个ZIP保持原样，不需重发或静默覆盖。同目录`guide/README.zh-CN.md`和`guide/VALIDATION.json`保留交付时“Windows未运行”的历史事实；本节是其后的新增验证记录，不改写过去。

首轮36752216453在OpenSSL路径前置检查失败，尚未运行候选包；后续选取单个真实命令路径。第二轮36752745701在新CI PIPE捕获方式下长时间未结束，最终状态仍待收集，不能直接认定具体停点。源码与独立普通子进程验证支持Windows长期CMD继承PIPE导致EOF等待的可能性，CI改为文件捕获并逐阶段保存证据；不改变已发guide或产品代码。上述历史失败/未完成运行保留，不算通过。

这是普通安装、SQL 和 AMQP 互操作演示，不代表 Windows 11、SSD、断电或生产验收。安全审阅、解析器/模糊测试与新增故障工作仍暂停。此改动不发布 release、不修改运行时代码，也不调整分支保护。

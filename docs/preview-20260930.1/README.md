# PG18 preview-20260930.1：Windows 普通流程验证

此目录保留已交付 `guide/` 的原始字节，不改变预览标签、包装器或候选二进制。真实扩展及候选包版本仍为 **0.1.0**，没有 0.1.2。固定输入和全部 guide 文件的 SHA256 见 `fixed-inputs.json`。

新增工作流 `preview-smoke.yml` 在 Windows Server 2022、Python 3.12、Node.js 24 上直接运行这份包装器。它用自动、只读 `GITHUB_TOKEN` 下载原 artifact 11099301285，分别核对外层 ZIP、原候选 ZIP 和原 `.sha256`，再使用 guide 自带的下载器取得固定官方 PostgreSQL 18.6。首次安装时，新建的 `echoo-preview-…` 根目录仅包含 `pg/`。

依赖安装使用附带锁文件和 `npm ci --ignore-scripts`，由 `node.exe` 直接运行同一安装目录的 `npm-cli.js`，不经过批处理或 shell；OpenSSL 使用 runner 现有命令的绝对路径。运行顺序为 `install → init → start → sql-demo → amqp-demo → stop`，并要求直接执行的 `pg_ctl status` 退出码为 3。流程不注册服务，不使用现有 PostgreSQL 或数据目录。

证据仅允许上传 `result.json`、经过凭据脱敏的 `stages.log` 与 `postgres.log`、以及不含消息正文的 `demo-results.json`。结果记录源码/脚本/归档哈希、实际子进程退出码、Node/npm CLI 路径、SQL 演示完成标记、rhea 三项成功记录和正常关闭状态。`sql_demo_marker` 指 `SQL_ENQUEUE_READ_ACK_OK`，不是原 `tests/sql/core.sql` 的完成标记，不计入原 core SQL 回归次数。失败时仅在确认本次新建目录身份且 PostgreSQL 确实运行后执行普通 `pg_ctl -m fast stop`；保留本机故障材料，不上传 PGDATA、证书、密码或整个运行根目录。

**当前状态：工作流已准备，尚无这份工作流在真实 Windows runner 上成功完成的证据。** `guide/README.zh-CN.md` 和 `guide/VALIDATION.json` 仍保留交付当时的“Windows 包装器未运行”状态。后续只有核对实际成功 run、对应提交和上传证据后，才能另行记录通过结果；不能借用原二进制 CI 的成功替代本次执行。

这是普通安装、SQL 和 AMQP 互操作演示，不代表 Windows 11、SSD、断电或生产验收。安全审阅、解析器/模糊测试与新增故障工作仍暂停。此改动不发布 release、不修改运行时代码，也不调整分支保护。

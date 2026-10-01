# ECHOO PGMQ · PG18 隔离测试预览

这份套件供本机、合成数据的初步接入测试。可以开始验证 SQL 入队、独立 AMQP 1.0 客户端收发、手动 ACK 与正常关闭后的重连。不要接真实 ERP、真实订单或现有 PostgreSQL 实例；这不是生产安装包。

## 版本与证据

- 此次“临时预览”是交付标签，不是扩展版本升级。原样候选 ZIP 的版本是 **0.1.0**，默认 SQL 扩展也是 **0.1.0**；包中附带可选的 0.1.1 SQL 升级脚本，**没有 0.1.2**。本指南不执行升级
- 固定源提交：`450681d71c2fefef0a92e57098643701dabcf4f5`
- 二进制来自 CI `36720731289` 的实际测试合并提交 `191d9947d181aab14667562513eae54109a7ea54`；已核对与固定源提交的 tree 同为 `d0abc20c68ce627015ba2934b38d0a600b812844`
- Linux / 原生 Windows Server 2022 × PG16/17/18，源码安装和原候选 ZIP 重装共 **464 项普通测试**，另有 12 次 core SQL 完成标记；这不是安全、故障或断电验收。PG18 每平台每轮 44 项，PG16/17 每平台每轮 36 项
- 普通 CI 验证的是上述固定二进制及原测试入口。新增 `preview.py` 已在本机 Debian 13 + 独立 PG18.6 上，使用这份原 Linux ZIP 完成 `install → init → start → sql-demo → amqp-demo → stop`，全部退出码为 0，SQL 标记、rhea 字节/metadata/远端 ACK/空队列检查通过，最后正常停止。这是额外普通演示证据，不能外推其他 Linux 发行版。**新包装器 Windows 路径尚未实机运行**，不能借用旧 Windows CI 宣称它已通过
- 安全审阅、解析器/模糊测试与新增故障测试继续暂停；人工审阅、真实 Windows 11 + SSD、物理断电等门槛仍未完成。项目许可证仍标为 `pending-owner-decision`，本包不授予未经确定的再分发许可

本次性能优化的目标是内部执行效率；这份固定预览保留已列出的外部 SQL/AMQP 行为。它不表示对任意 AMQP 客户端的兼容性保证，也不承诺今后发现正确性问题时永不调整接口。

## 文件、依赖和运行目录

每个平台的套件应保留原候选 ZIP、同名 `.zip.sha256` 与本 `guide` 目录。不要拆开 DLL 后复制到已有 PostgreSQL。`preview.py install` 会校验固定 ZIP 哈希与内部逐文件 manifest，再调用原仓库 `install_candidate.py`；原安装器会替换目标 prefix 的 Echoo/Proton 文件，所以必须使用**全新的独立二进制 prefix**。

运行时目录必须命名为 `echoo-preview-…`，首次安装时只允许包含 `pg` 子目录：

```text
echoo-preview-450681d-win/     # 自己账户下的私有目录，Linux 可另用 -linux
  pg/                        # 独立 PostgreSQL 18.6 x64 全套二进制及 share/lib
  data/                      # init 新建，绝不能是现有 PGDATA
  certs/                     # init 本机生成临时 CA/证书/私钥，有效期 2 天
  .pgpass-preview            # 本机临时 SCRAM 凭据，切勿发送/上传
  preview-state.json         # 目录身份和本机端口，不含密码
  postgres.log
  demo-results-….json         # 普通 AMQP 演示结果，不含证书/私钥/消息正文
```

运行账户需能写这个私有目录，其他不可信账户不应能读取或修改它。Linux 用普通用户执行，不能用 root。Windows 用自己的普通账户目录，不要放 Public/共享盘；沿用可信父目录的正常访问权限，不关闭 PostgreSQL 的受限令牌保护。

所需依赖：

| 项目 | 要求与范围 |
|---|---|
| PostgreSQL | 本包参考版本 18.6、x86-64；Windows 为原生 x64。`pg_config --bindir/--pkglibdir/--sharedir` 均须在私有 `ROOT/pg` 内，包装器会拒绝越界 |
| Windows DLL | 原包带 Proton 0.40.0；不带 OpenSSL 或 Microsoft 运行库。Proton DLL 依赖 `libssl-3-x64.dll`、`libcrypto-3-x64.dll`、`MSVCP140.dll`、`VCRUNTIME140.dll`、`VCRUNTIME140_1.dll`、UCRT。参考 CI OpenSSL 为 3.6.4，不能只凭“也是 PG18”判定任意发行版都兼容 |
| Linux ABI | 参考 CI 为 Ubuntu 24.04 x86-64、glibc 2.39、OpenSSL 3。不同发行版/架构/库版本须重新验证；不是通用 Linux 二进制 |
| Python | 3.12 或更新版本，包装器仅使用标准库。只用于安装辅助及演示，服务本身不需要 Python |
| OpenSSL 命令 | 可信现有发行版的 `openssl`，用于本机生成临时测试证书；可用 `ECHOO_TEST_OPENSSL` 指定绝对路径。不要从 DLL 下载站补文件，不覆盖 PostgreSQL 自带 SSL DLL |
| Node.js/npm | Node.js 24；仅独立客户端演示需要。rhea 3.0.5、debug 4.4.3、ms 2.1.3 由附带 lockfile 固定；服务本身不需要 Node |

如缺 Microsoft 运行库，应由你按 [Microsoft 官方安装说明](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170)准备与 x64 应用匹配的 v14 运行库。脚本不安装系统软件、不修改系统 PATH、不注册服务、不改变防火墙。[Python 官方下载](https://www.python.org/downloads/)与 [Node.js 官方下载](https://nodejs.org/en/download)可用于准备辅助工具。

## Windows：首次准备与安装（主要路线）

先将套件解压到自己的工作目录。在 PowerShell 进入其中的 `guide` 目录。以下命令一次执行一段；出错即停，不忽略退出码。整个实验的目录路径确定后不要移动。

```powershell
$ErrorActionPreference = 'Stop'
function Invoke-Py { & python @args; if ($LASTEXITCODE -ne 0) { throw 'Python command failed; stop and inspect its output' } }
$Guide = (Get-Location).Path
$Root = Join-Path $env:USERPROFILE 'echoo-preview-450681d-win'
$Archive = (Resolve-Path '..\echoo-pgmq-0.1.0-candidate-pg18-windows-amd64.zip').Path
if (Test-Path $Root) { throw 'Choose a fresh ROOT; do not overwrite an earlier preview' }
New-Item -ItemType Directory $Root | Out-Null

# 原仓库帮助程序：下载官方 EDB PG18.6 x64 ZIP，先比对固定 SHA-256，再展开到新目录
Invoke-Py .\scripts\fetch_dependency.py postgres-windows-18 "$Root\pg"
& "$Root\pg\bin\postgres.exe" --version
if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL runtime prerequisite failed' }
& "$Root\pg\bin\initdb.exe" --version
if ($LASTEXITCODE -ne 0) { throw 'initdb runtime prerequisite failed' }
& "$Root\pg\bin\psql.exe" --version
if ($LASTEXITCODE -ne 0) { throw 'psql runtime prerequisite failed' }

# 仅当前进程环境变量，用于本机生成证书，不写系统 PATH
$env:ECHOO_TEST_OPENSSL = (Get-Command openssl.exe -ErrorAction Stop).Source
Invoke-Py .\preview.py install --root "$Root" --archive "$Archive" --disposable-installation
```

`fetch_dependency.py` 固定官方 EDB URL 和 SHA-256；来源记录在 `HELPER-PROVENANCE.json`。它不创建系统服务，目标 `ROOT/pg` 必须不存在。若下载地址不可用或校验不匹配，停在这里，不换未经核验的镜像或手工放宽哈希。

如果 `openssl.exe` 尚不在当前会话可用路径，先准备可信的 OpenSSL 工具，再把 `ECHOO_TEST_OPENSSL` 设为其真实完整路径。若返回 `0xC0000135` 或提示缺 DLL，这是 Windows loader/依赖问题，不能当作 SQL/AMQP 失败；先核对以上运行库及 PG 自带 DLL。`scripts/check_windows_runtime.py ROOT/pg` 可做原仓库诊断，包含临时空目录中的 `initdb` 受限令牌检查，结束会删除该空探针目录，**不会启动监听器**；它不是新包装器的 Windows 实跑证据。

准备独立客户端依赖：

```powershell
& node --version
if ($LASTEXITCODE -ne 0) { throw 'Node.js 24 is required' }
& npm.cmd ci --prefix .\interop --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org
if ($LASTEXITCODE -ne 0) { throw 'Locked npm dependency installation failed' }
& node .\interop\verify_dependencies.js
if ($LASTEXITCODE -ne 0) { throw 'Dependency verification failed' }
```

## Linux：独立 prefix 准备与安装

推荐与 CI 一致的 Ubuntu 24.04 x86-64 测试环境。不能把 `--pg-config` 指向 `/usr/lib/postgresql/18/bin/pg_config` 等共享系统安装。本包装器固定使用 `ROOT/pg/bin/pg_config`。

已有真正独立的 PG18.6 prefix 时，可将其置于新 `ROOT/pg`，但必须确认其三个 `pg_config` 输出都留在该目录。不要复制或链接正在运行的 PostgreSQL 安装。若需自行准备，可从 [PostgreSQL 官方 18.6 源码目录](https://www.postgresql.org/ftp/source/v18.6/)下载 `postgresql-18.6.tar.gz` 和同名 `.sha256`，先执行 `sha256sum -c postgresql-18.6.tar.gz.sha256`，再解压编译到私有 prefix。以下为示例；源码构建需要你已准备好 PostgreSQL 对应的编译依赖，其产物仍需本机普通验证：

```bash
set -euo pipefail
GUIDE="$PWD"                        # 当前在解压后的 guide 目录
PREVIEW_ROOT="$HOME/echoo-preview-450681d-linux"
ARCHIVE="$(realpath ../echoo-pgmq-0.1.0-candidate-pg18-linux-x86_64.zip)"
test ! -e "$PREVIEW_ROOT"
mkdir -m 700 "$PREVIEW_ROOT"

# 在另一个已校验并解压的 PostgreSQL 18.6 源码目录执行下面三行。
# 源码/构建目录放在 PREVIEW_ROOT 外面，ROOT 初次安装只应有 pg。
# ./configure --prefix="$PREVIEW_ROOT/pg" --with-openssl
# make -j2
# make install

"$PREVIEW_ROOT/pg/bin/pg_config" --version
"$PREVIEW_ROOT/pg/bin/pg_config" --bindir
"$PREVIEW_ROOT/pg/bin/pg_config" --pkglibdir
"$PREVIEW_ROOT/pg/bin/pg_config" --sharedir
cd "$GUIDE"
python3 preview.py install --root "$PREVIEW_ROOT" --archive "$ARCHIVE" --disposable-installation

# 仅对已核验、已安装的本候选执行依赖检查；输出不能有 not found。
PKGLIB="$("$PREVIEW_ROOT/pg/bin/pg_config" --pkglibdir)"
ldd "$PKGLIB/echoo_pgmq.so"
ldd "$PKGLIB/libqpid-proton.so.11"
npm ci --prefix interop --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org
node interop/verify_dependencies.js
```

不同 PG 构建的 `pkglibdir` 可能不同；上面命令使用实际 `pg_config --pkglibdir` 输出。不要通过随机增加库路径来掩盖 ABI 缺失，先按本机发行版的可信依赖来源处理。此处不自动下载或安装系统库。

## 初始化、启动和正常演示

`init` 仅允许全新 `data` 和 `certs`，会生成本机随机 SCRAM 密码与两天有效的测试证书。客户端证书 CN 固定 `echoo_test_user`，与新建数据库角色精确匹配。密码不输出到终端，私钥不打包。

PostgreSQL 固定只监听 `127.0.0.1:55418`，AMQP 只监听 `127.0.0.1:56718`；本机已占用端口时，可在首次 `init` 时指定 `--pg-port` 与 `--amqp-port` 为其他不同端口，仍不改变监听地址。客户端使用 `amqps://localhost:端口`，验证 CA、服务器名与客户端证书。`init` 会短暂启动空数据库完成角色/扩展设置，然后正常 fast 停止；`start` 才启动完整 AMQP 预览。

Windows PowerShell：

```powershell
Invoke-Py .\preview.py init --root "$Root"
Invoke-Py .\preview.py start --root "$Root"
Invoke-Py .\preview.py status --root "$Root"
Invoke-Py .\preview.py sql-demo --root "$Root"
Invoke-Py .\preview.py amqp-demo --root "$Root"
Invoke-Py .\preview.py stop --root "$Root"
```

Linux：

```bash
python3 preview.py init --root "$PREVIEW_ROOT"
python3 preview.py start --root "$PREVIEW_ROOT"
python3 preview.py status --root "$PREVIEW_ROOT"
python3 preview.py sql-demo --root "$PREVIEW_ROOT"
python3 preview.py amqp-demo --root "$PREVIEW_ROOT"
python3 preview.py stop --root "$PREVIEW_ROOT"
```

启动日志应出现 `AMQP 1.0 mutual-TLS listener started`。`pg_ctl start` 成功只表示 PostgreSQL 已启动；AMQP 是否可用还要看该日志和实际 `amqp-demo` 成功，不能把进程存活当协议验收。

预期输出：

- `sql-demo`：新建合成队列，用非管理员 `echoo_test_user` 会话在同一个 SQL 事务内写一行临时示例订单并 `enqueue_binary`；读取后核对正文，通过 `id + generation + owner` 完整 receipt ACK；输出 `SQL_ENQUEUE_READ_ACK_OK`，再核对队列和消息行都为零
- `amqp-demo`：3 个小规模场景，每次新建合成队列。每个场景先以 rhea 发送并等远端 Accepted，再通过 SQL 核对完整 encoded message 与存储字节相等；随后执行 roundtrip、Released 后重投、正常 AMQP Close 后新连接重投，均采用手动 SECOND ACK 并等远端 settlement，最后核对队列清空
- 重连是正常关闭后建立新连接、新 link，等 2 秒租约过期后重投；不是断网恢复、强杀进程、持久 link 恢复或断电测试

每次演示只使用合成数据。可以重复执行演示，队列名带随机后缀；消息会 ACK 清空，空队列与少量 SQL 幂等记录保留在实验数据库。任一步失败时目录与日志保留，先排查，再明确正常停止；包装器不会强杀数据库，也不会自动删除数据。

## 接入时要保持的外部约定

SQL 业务入队优先使用 `enqueue_binary`，它把应用 bytea 包装为合法 AMQP data section。`enqueue` 是完整 encoded AMQP message 的专家原始接口，不能直接把 JSON 文本字节塞进去并认为客户端一定会正确解码。

```sql
-- 队列应先由实验管理员创建，并 grant_queue 给此连接的 session_user。
BEGIN;
-- 此处仅演示：真实接入中，自己的业务 SQL 与下面入队处于同一 PostgreSQL 事务。
SELECT echoo_pgmq.enqueue_binary(
  'preview/orders', convert_to('{"order_id":42,"synthetic":true}', 'UTF8'),
  'order-42-created');
COMMIT;
```

上面是接入形状示例，固定 `preview/orders` 不由脚本自动创建；实际可运行示例请直接执行 `sql-demo`。SQL API 按 `session_user` 授权，`SET ROLE` 不是模拟另一个已授权用户的方法。

AMQP 只实现已声明的 **AMQP 1.0 持久 work queue 子集**：精确队列名（如 `preview/orders`），无动态建队列、exchange/topic fanout、RabbitMQ 0-9-1、selector、AMQP 事务或 durable link resumption。队列由管理员创建，证书 CN → 同名 PostgreSQL 角色 → 显式 `grant_queue` ACL；SASL ANONYMOUS/EXTERNAL 或直接 AMQP 不会替代证书身份。不是用户名/密码 AMQP URL。

服务端使用 Proton C 0.40.0 + OpenSSL/PEM。本套件独立客户端限定 Node.js 24 + rhea 3.0.5；Python Proton 原 Windows 客户端的 SChannel/P12 配置与此 PEM 路线不同。不能从这些结果外推任意 .NET/JMS/Rust 客户端已兼容。

生产者 Accepted 在数据库同步提交之后发出；消息和队列数据存入 PostgreSQL WAL 表，配置保持 `fsync=on`、`full_page_writes=on`、`synchronous_commit=on`。这是持久化设计与正常测试覆盖，不等于物理断电证明。语义是**至少一次**：提交后未收到 Accepted、业务处理后未提交 ACK 等情形均可能重复，业务必须幂等；AMQP 的 message-id 不提供自动去重。消费者应先完成自己的幂等处理，再 ACK。这个预览用 2 秒消费租约做短演示，不适用于耗时真实任务。

## 正常停止、保留与明确清理

`stop` 使用 PostgreSQL `pg_ctl -m fast -w stop`，正常结束事务/worker 并保留数据、日志与证书。它不会卸载或删目录。再次 `start` 使用原数据；证书两天后过期，重新建立一套新的隔离预览更适合初测，不要关闭证书验证继续用。

清理时，先执行相应平台的 `stop`，再用 `ROOT/pg/bin/pg_ctl[.exe] -D ROOT/data status` 确认提示没有服务器运行（该命令正常返回码为 3）。如果停止失败、status 不确定或实验路径不明确，保留目录排查。然后由你在文件管理器中**明确删除这一个实验 ROOT**，它同时包含数据库数据、临时 CA 和所有私钥；不要只删 `data` 而遗留 `certs`、`.pgpass-preview`，也不要误删套件、系统 PostgreSQL 或别人的目录。

需要反馈时，优先给出平台/PG/Node 版本、失败步骤和错误信息；AMQP 的 `demo-results-….json` 不含凭据。不要上传整个 ROOT、PGDATA、证书私钥、`.pgpass-preview` 或真实业务数据。所有安全/解析器/模糊/故障资格工作流仍保持暂停，本指南不触发它们。

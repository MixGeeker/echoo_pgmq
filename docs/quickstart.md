# 快速开始

以下用于隔离开发/验收环境。不要直接对现有 ERP 生产 PostgreSQL 复制配置或执行 crash 测试。0.1.2 是候选版本；部署前阅读 [安全策略](../SECURITY.md) 与 [支持边界](support.md)。

## 1. 编译与安装

目标为 x86-64 Linux、原生 Windows；PostgreSQL 18（主要目标）及16/17 分别构建。需要对应服务器头文件与 import library（Windows）、CMake ≥3.20、C11 编译器、Apache Proton 0.40.0 和 OpenSSL。Python ≥3.12 仅用于构建辅助/测试，不是服务运行依赖。

Linux 的完整最短命令在 [README](../README.md)。CMake 的 `PG_CONFIG` 必须指向目标实例的开发安装，`PROTON_ROOT` 指向以 OpenSSL 后端构建的 Proton。运行 `cmake --install build` 默认安装到 pg_config 输出的 pkglibdir 与 sharedir/extension。

Linux 运行时需找到 `libqpid-proton.so.*` 与 OpenSSL。开发可设置 LD_LIBRARY_PATH；生产应使用受保护的系统库目录或把候选中的 Proton 库放到扩展同目录（安装 RPATH 为 `$ORIGIN`）。不要把临时构建目录的 LD_LIBRARY_PATH 当作生产方案。

原生 Windows 的示例（Visual Studio 2022 x64 工具链、原生 PostgreSQL 开发文件与 OpenSSL 已安装）：

```powershell
python scripts/fetch_dependency.py proton C:/echoo-build/proton-src
python scripts/patch_proton_windows.py C:/echoo-build/proton-src
cmake -S C:/echoo-build/proton-src -B C:/echoo-build/proton-build -A x64 `
  -DCMAKE_INSTALL_PREFIX=C:/echoo-build/proton -DSSL_IMPL=openssl -DSASL_IMPL=none `
  -DOPENSSL_ROOT_DIR="C:/Program Files/OpenSSL" -DBUILD_CPP=OFF -DBUILD_PYTHON=OFF `
  -DBUILD_RUBY=OFF -DBUILD_GO=OFF -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF -DBUILD_TOOLS=OFF
cmake --build C:/echoo-build/proton-build --config RelWithDebInfo
cmake --install C:/echoo-build/proton-build --config RelWithDebInfo
cmake -S . -B build -A x64 `
  -DPG_CONFIG="C:/Program Files/PostgreSQL/18/bin/pg_config.exe" `
  -DPROTON_ROOT=C:/echoo-build/proton
cmake --build build --config RelWithDebInfo
cmake --install build --config RelWithDebInfo
```

原生 PostgreSQL ZIP 还依赖 Microsoft Visual C++ 运行库与 Universal CRT。先直接执行目标 `initdb.exe --version`、`postgres.exe --version` 和 `psql.exe --version`，确认 PostgreSQL 本身能够加载，再排查扩展。若返回 `0xC0000135`，说明进程加载依赖失败，不能当成扩展的 SQL/AMQP 失败。CI 的 `scripts/check_windows_runtime.py` 会输出依赖加载诊断；版本输出不会覆盖 initdb 的受限令牌重新执行路径，因此还需在可丢弃目录实际初始化一次。不要把 Python 私有临时解压目录整体移动成运行目录：Windows 的 OWNER RIGHTS ACL 可能只允许提升权限后的安装进程读取；下载脚本会先在私有目录核验官方归档，再将公开运行文件复制到继承安装父目录正常 ACL 的新目录。SDK 属于开发工具，不是门店运行的强制前提；运行库来源与部署要求见 [Microsoft 官方说明](https://learn.microsoft.com/en-us/cpp/windows/universal-crt-deployment?view=msvc-170)。不要从第三方 DLL 下载站补文件、覆盖 PostgreSQL 自带 DLL、关闭受限令牌，或把构建机临时 PATH 直接当作生产服务配置。

如 CMake 未找到 `postgres.lib`，使用 `-DPOSTGRES_LIBRARY=完整路径`。启动 PostgreSQL 的服务进程必须能找到 Proton/OpenSSL DLL；交互式 PowerShell 的 PATH 不等于服务 PATH。不要盲目覆盖 PostgreSQL 自带 SSL DLL，核对依赖 ABI/架构并限制 DLL 目录写权限。首版服务端固定使用 OpenSSL/PEM，不支持默认 SChannel/PFX 的服务端配置。

## 2. 数据库与角色

先安装扩展与角色，之后启用 listener 并重启。由管理员执行：

```sql
CREATE EXTENSION echoo_pgmq VERSION '0.1.2';
CREATE ROLE echoo_pgmq_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE erp_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT USAGE ON SCHEMA echoo_pgmq TO echoo_pgmq_worker;
GRANT EXECUTE ON FUNCTION
  echoo_pgmq.publish(text,bytea,text),
  echoo_pgmq.claim(text,text,uuid,integer),
  echoo_pgmq.settle(text,bigint,bigint,uuid,text,text),
  echoo_pgmq.authorize(text,text,text)
TO echoo_pgmq_worker;
SELECT echoo_pgmq.create_queue('erp/orders', 10000, 67108864, 1048576, 5);
SELECT echoo_pgmq.grant_queue('erp/orders', 'erp_app', true, true);
```

PostgreSQL16 的 background worker 不支持 NOLOGIN 绕过：将该版本 worker 建为 LOGIN（不设置密码），并在 pg_hba.conf 最前面加入 `host all echoo_pgmq_worker 0.0.0.0/0 reject`、`host all echoo_pgmq_worker ::0/0 reject`，Linux 另加 `local all echoo_pgmq_worker reject`。PostgreSQL17/18 保持 NOLOGIN。更改 pg_hba 后按 PostgreSQL流程 reload；不要向业务用户授予 worker 成员资格。

`erp_app` 的 PostgreSQL 登录认证按现有 pg_hba.conf/身份管理配置；不要在生产使用 trust。AMQP 的认证独立使用证书，不能把数据库密码放到 AMQP URL。

## 3. 证书与配置

使用专用 CA 签发服务器证书（含正确 DNS/IP SAN）与客户端证书（clientAuth EKU，CN=`erp_app`）。客户端必须验证服务器名称与 CA。角色名/CN 区分大小写并精确匹配；建议只使用小写字母、数字与下划线。禁止共用管理员证书。

`postgresql.conf`：

```conf
shared_preload_libraries = 'echoo_pgmq' # 若已有扩展，追加，不覆盖现有列表
fsync = on
full_page_writes = on
synchronous_commit = on

echoo_pgmq.enabled = on
echoo_pgmq.database = 'erp'
echoo_pgmq.role = 'echoo_pgmq_worker'
echoo_pgmq.listen_address = '127.0.0.1'
echoo_pgmq.port = 5671
echoo_pgmq.tls_certificate = '/protected/echoo/server.pem'
echoo_pgmq.tls_private_key = '/protected/echoo/server.key'
echoo_pgmq.tls_ca_file = '/protected/echoo/client-ca.pem'
```

Windows 路径建议使用 `C:/...`。全部扩展 GUC 为启动参数，修改/轮换证书后需重启 PostgreSQL。启动日志应出现 `AMQP 1.0 mutual-TLS listener started`。未配置有效证书时失败关闭，没有明文降级。

## 4. SQL 业务事务入队

以已授权业务角色连接，使用 `enqueue_binary` 包装二进制正文为合法 AMQP 消息（不是把 JSONB 当作传输格式）：

```sql
BEGIN;
-- 在这里执行真实业务更新；示例不创建或更改你的 ERP 表。
SELECT echoo_pgmq.enqueue_binary('erp/orders', convert_to('{"order_id":42}', 'UTF8'), 'order-42-created');
COMMIT;
```

业务更新与入队共享事务：业务回滚不会留下消息。**`enqueue` 是不校验正文编码的高级原始bytea接口**，若用于AMQP，调用者必须传入完整合法encoded message。任意bytes可能被客户端宽松解码为空消息，或导致解析异常；不能把未校验正文当作业务成功并ACK。服务端消费路径会对非法编码、空或超过worker上限的存储消息直接保留为死信，避免阻塞后续合法消息；合法消息的未确认重试才按attempt上限耗尽。SQL 消费可以处理任意 bytea，但如果预期 AMQP 消费，优先使用 `enqueue_binary` 或客户端编码。

SQL 消费的 `read` 返回 id、generation、body，调用者提供 UUID owner；ACK 必须带回这三个 receipt 字段。不要只根据 message id 删除消息。详见 [存储文档](storage.md)。

## 5. Python AMQP 1.0 示例

安装 `python-qpid-proton==0.40.0`。以下为 Linux/OpenSSL 客户端，证书路径应替换为已签发路径：

```python
from proton import Message, SSLDomain
from proton.utils import BlockingConnection

ssl = SSLDomain(SSLDomain.MODE_CLIENT)
ssl.set_credentials('/protected/erp_app.pem', '/protected/erp_app.key', None)
ssl.set_trusted_ca_db('/protected/server-ca.pem')
ssl.set_peer_authentication(SSLDomain.VERIFY_PEER_NAME)
conn = BlockingConnection('amqps://localhost:5671', ssl_domain=ssl,
                          sasl_enabled=False, reconnect=False, timeout=10)
try:
    sender = conn.create_sender('erp/orders')
    sender.send(Message(body=b'\x00\xfforder', properties={'order_id': 42}))
    receiver = conn.create_receiver('erp/orders', credit=1)
    message = receiver.receive(timeout=10)
    # 先完成幂等业务处理，再确认。外部副作用不在 AMQP ACK 的数据库事务内。
    receiver.accept()
    receiver.close()  # 处理 close/settlement 结果；生产程序必须记录并重试失败
finally:
    conn.close()
```

Python Proton 的原生 Windows 构建使用 SChannel，客户端凭据需 PKCS#12 + friendly name，CA 也需匹配后端格式。集成测试已分别准备客户端 P12 与服务端 PEM；不能把两端配置混淆。

## 6. 自动化验证

```sh
python -m pip install -r requirements-dev.txt
python scripts/run_integration.py --ordinary --pg-config /path/to/pg_config --work-dir /tmp/echoo-validation --keep
```

`--ordinary` 只运行明确列出的普通 SQL/AMQP/归档回归，使用正常 fast 启停；不运行畸形协议、资源耗尽、进程强杀或提交故障注入。完整 qualification 当前暂停于安全审阅，普通回归通过不等于完整验收。必须以普通 OS 用户运行，工作目录必须没有既存 data/certs。不要指向生产目录。保留的测试目录包含私钥，手动安全清理。

PG18的独立JavaScript客户端验证额外使用Node.js 24与锁定的rhea3.0.5。安装命令、默认FIRST/手动SECOND、释放与正常重连场景见[独立客户端说明](https://github.com/MixGeeker/echoo_pgmq/blob/a501852e2137f3f46e710384a8ada9c4315f9939/tests/interop/README.md)。这些仅为开发依赖，PostgreSQL服务不需要Node/Python进程。

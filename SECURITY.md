# 安全策略

## 状态与报告

0.1.0 是开发候选，尚未经过独立渗透测试、长期运行验证或生产安全认证。发现疑似漏洞时，请使用仓库 GitHub 的私密漏洞报告功能（若已开启），或联系仓库所有者协商私密渠道。不要把证书私钥、生产消息、数据库转储、连接口令或可利用的生产细节贴到公开 issue。当前没有承诺响应 SLA 或安全版本支持期限。

报告请包含：受影响提交、PostgreSQL/OS/Proton/OpenSSL 版本、最小复现步骤、预期与实际行为、权限前提、已脱敏日志及影响。不要对不属于自己的服务进行测试。

## 信任边界

- listener 是 PostgreSQL 的原生 C background worker。协议解析的内存安全缺陷或非法内存访问可能触发整个 PostgreSQL 集群的崩溃恢复，影响同机业务连接
- 因此只应把监听端口暴露给受信任应用网段；默认 `127.0.0.1`。防火墙、独立服务账户、升级补丁与网络隔离不能省略
- 强制 TLS 1.2/1.3 与客户端证书。服务端使用 Proton OpenSSL 后端；Windows 默认 SChannel 后端不属于本扩展首版受测服务端配置
- 证书 CN 精确匹配保守 ASCII 数据库角色名。使用专门签发队列客户端证书的 CA，限制签发权限；一张获信 CA 签发的证书可能代表其 CN 对应主体
- SAN URI/外部 IdP、动态身份映射、证书热重载与 OCSP/CRL 自动吊销流程尚未实现。不要声称“证书吊销后会立刻中断已有会话”
- SASL 的 ANONYMOUS/EXTERNAL 只是协商机制；授权身份仍仅来自经验证的 TLS 证书。客户端自行填写的 SASL 用户名、AMQP container-id、message user-id 不能提升权限

## 数据库权限

扩展由受信任管理员安装。PostgreSQL17/18 的 worker 使用专用 `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION` 角色；只授予 schema USAGE 与文档列出的四个私有接口。PostgreSQL17/18 原生 worker 显式绕过该角色的 LOGIN 检查来建立内部连接，普通网络客户端不能因此登录。PostgreSQL16 无此绕过标志，必须使用不设密码的 LOGIN worker 角色，同时在 pg_hba.conf 通用规则之前显式 reject 该角色的所有TCP/Unix-socket外部登录（详见快速开始）；不能把 NOLOGIN17 的保证套用到16。

业务角色只获得公开 enqueue/read/ack/release/reject 接口，通过 session_user 与队列 ACL 授权。不向业务角色授予底表、序列或私有 `publish/claim/settle/authorize` 的权限。不把 worker 角色授予业务角色，不使用超级用户运行 listener。SECURITY DEFINER 函数固定 search_path；管理员仍应禁止不可信用户修改扩展 schema。

CN 与数据库角色一一对应不意味着所有现有数据库角色默认可访问队列；必须显式 grant_queue。撤销 ACL 可阻止后续操作，但需要切断连接/重启 listener 才能强制会话级立即失效。

## 私钥、动态库与敏感内容

- 服务端 PEM 私钥当前无口令加载；Linux 使用 owner-only 权限，Windows 使用最小 NTFS ACL，仅 PostgreSQL 服务身份与受信管理员可读
- 不把私钥放进仓库、GitHub artifacts、日志或配置示例。测试 CA 仅有效两天，绝不能用于生产
- 原生库、Proton DLL、OpenSSL DLL 的搜索目录不能由普通用户写入。Windows 不盲目覆盖 PostgreSQL 发行包附带的 OpenSSL DLL；核对 ABI、版本和加载路径
- TLS 不提供磁盘加密。WAL、物理备份、逻辑导出和死信都包含消息，必须按业务数据保护
- 协议错误不返回消息正文与数据库内部参数；日志只用于诊断。仍须审查日志是否包含应用自行记录的敏感内容

## 耐久性与可用性

worker 要求 `fsync=on`、`full_page_writes=on`，并在自己的事务中强制 `synchronous_commit=on`。SQL 业务事务仍需由调用方保证同等设置。Accepted 的保证以 PostgreSQL、操作系统、文件系统与存储设备正确实现持久化屏障为前提；UPS、SSD 缓存策略、磁盘故障与真实掉电应单独验证。

资源上限不是完整 DoS 证明。除了连接、frame、session、link、inflight、缓冲区与表容量限制，还需磁盘告警、连接来源限制、主机内存限制与压力测试。大量合法连接或队列锁竞争仍能降低单 worker 吞吐。

## 供应链

GitHub Actions 固定 commit SHA，默认仅 `contents: read`，checkout 不保留凭据。上游 Proton 与 Windows PG 下载先核对仓库固定的散列。OS 包使用发行方签名源，Python 包固定直接版本；构建环境及传递依赖并非完整 hermetic/reproducible-build 锁定。候选包含文件 SHA-256 与依赖许可，但未代码签名，也未附带第三方安全审计证明。

## 当前审查状态（2026-09-30）

自动检查已在提交d24d57a通过：GCC静态分析、限定时间的Proton解析器ASAN/UBSAN fuzz、Python依赖公告审计。它们只证明列明检查的结果。

人工安全审查尚未完成。协议解析深度的静态候选已复现并修复：Proton 0.40 的编解码器无深度上限地递归解码复合值，单条不足 1 MiB 的深层嵌套 list 即可耗尽 C 栈（独立复现约 10 万层时 SIGSEGV）；AMQP 收件与 SQL 入队消息的投递前校验现在先以有界递归遍历编码，嵌套超过 64 层（described/list/map/array 各计一层）直接拒绝，Proton 不再接触该输入。解码后节点数的内存放大（例如大量 1 字节空值）仍只受 max_message_bytes 约束，尚未设独立上限；不能把自动CI通过视为已消除其余风险。相关解析器调查、模糊测试和新的故障注入现已暂停；安全发布闸门仍未通过，产品不得宣称可生产发布。

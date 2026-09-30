# 项目与依赖许可

## 项目许可待定

仓库所有者尚未选择 Echoo PGMQ 的发行许可证。本仓库故意不添加 MIT/Apache/GPL 等项目 LICENSE，也不替所有者决定未来商业授权模式。当前源码可供本次项目开发与审阅，但不能从公开可访问或存在候选 ZIP 推导出已获开源再分发许可。发布前必须明确许可、版权持有人与贡献政策。

## 运行/构建依赖

- PostgreSQL：PostgreSQL License；官方 https://www.postgresql.org/about/licence/
- Apache Qpid Proton 0.40.0：Apache License 2.0；分发其二进制时保留 LICENSE/NOTICE。来源与下载验证 https://qpid.apache.org/releases/qpid-proton-0.40.0/
- OpenSSL 3.x：Apache License 2.0；实际使用版本以构建清单/操作系统发行包为准 https://openssl-library.org/source/license/
- Python Qpid Proton、pytest、psycopg 等为开发/测试依赖，许可应按锁定版本的 package metadata/上游 LICENSE 逐一核对。psycopg-binary 的打包内容可能包含额外依赖，候选包不携带 Python 测试环境

Windows构建会对固定的Proton0.40.0官方源应用可审计的OpenSSL/MSVC兼容补丁；脚本先核对受改文件SHA256，保留Apache头部并生成ECHOO_WINDOWS_OPENSSL_PATCH.txt变更说明，随候选一起分发。

候选打包脚本要求携带 Proton LICENSE/NOTICE，不把“链接了某库”视为许可核验完成；真实发布前仍需形成完整第三方清单与分发审查。安装 PostgreSQL/OpenSSL 的二进制发行包也需遵守其自身条款。

## 设计参考与实现来源

PostgreSQL 队列/可见性租约/代次确认等设计思想可以独立实现。首版没有直接复制 Kafgres 或 PGMQ 源代码。尤其 Kafgres 的 Elastic License 2.0 不能简单当作宽松许可证；若未来引入其代码，需单独审阅及保留通知。任何后续复制、衍生或新增依赖均需重新审阅，不能沿用本段作为自动许可。

# 项目与依赖许可

## 项目许可：Apache-2.0

本项目自行提供、未另行标注的源代码、文档及构建文件采用标准[Apache License 2.0](../LICENSE)。许可证正文与[Apache官方原文](https://www.apache.org/licenses/LICENSE-2.0.txt)逐字节一致，未加入额外限制：11,358字节，SHA-256为`cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`。保留末尾的标准应用示例占位符；本项目实际署名独立写在[NOTICE](../NOTICE)：Copyright 2026 MixGeeker。

该署名只用于Echoo PGMQ项目，不宣称拥有PostgreSQL、Proton、OpenSSL或其他第三方组件的版权，也不改变它们的许可证或既有通知。贡献者保留其版权；Apache-2.0第5条按其原文处理主动提交的贡献，本次未添加CLA或新的权利转让条款。仓库现有独立实现说明与这次通知核对，不构成完整历史权属、专利或代码相似性审计的保证。

### 可选的关于页致谢

建议集成方在“关于”或通常的“第三方声明”界面写明：使用Echoo PGMQ（MixGeeker），Apache-2.0，并链接本项目。**这是可选建议，不是额外许可条件；没有强制关于页、位置、字样、标志或宣传要求。**

实际分发义务以LICENSE第4条为准，包括提供许可证副本、对修改作说明、保留适用的来源通知等。对于适用的NOTICE归属通知，第4(d)条提供随分发NOTICE文件、源码/文档或通常的第三方声明界面等选项。仅加一句致谢不自动替代这些义务；NOTICE的信息本身不修改许可证。

### 新包与冻结预览的边界

新的候选归档必须包含项目LICENSE、NOTICE，并在MANIFEST中记录`project_license=Apache-2.0`及两份文件散列；Proton原始LICENSE/NOTICE和适用的Windows修改说明另放在third-party/，不得改成MixGeeker署名。缺少项目法律文件时打包失败，不静默省略。源码构建后若另行分发裸so/DLL，应同时提供适用的许可证和通知，不能仅复制二进制就假定履行分发义务。

已发preview-20260930.1/.2 ZIP及guide字节不变，其manifest中的pending-owner-decision保留为历史构建记录。本次不重写这些归档、不用同一预览号替换字节；本许可证与通知随此次源码变更及后续新包提供。项目许可证确定不代表完整第三方分发审查、安全审核或生产资格已经完成。

当前CMake与隔离安装器只复制运行所需模块/SQL等文件，不会把这些许可文档安装进PostgreSQL目录。文档保留在源码根目录、候选ZIP的根目录及third-party/；请保留该归档或一同解压的许可文档。若打包另一个可分发安装目录，应主动把适用通知一同放入该分发物，不能依赖安装器自动完成。

## 运行/构建依赖

- PostgreSQL：PostgreSQL License；官方 https://www.postgresql.org/about/licence/
- Apache Qpid Proton 0.40.0：主许可证为Apache License 2.0，上游LICENSE还包含子组件说明/许可；保留完整原文，不把每个上游文件都视为同一许可证。分发其二进制时保留适用的原始LICENSE/NOTICE。来源与下载验证 https://qpid.apache.org/releases/qpid-proton-0.40.0/
- OpenSSL 3.x：Apache License 2.0；实际使用版本以构建清单/操作系统发行包为准 https://openssl-library.org/source/license/
- Python Qpid Proton、pytest、psycopg 等为开发/测试依赖，许可应按锁定版本的 package metadata/上游 LICENSE 逐一核对。psycopg-binary 的打包内容可能包含额外依赖，候选包不携带 Python 测试环境

### 独立JavaScript互操作测试依赖

这些依赖只位于`tests/interop`开发测试环境，不打包到候选扩展，也不是PostgreSQL服务的Node.js运行时依赖：

| npm包 | 锁定版本 | 许可 | 官方来源 |
|---|---|---|---|
| rhea | 3.0.5 | Apache-2.0 | https://github.com/amqp/rhea |
| debug（rhea传递依赖） | 4.4.3 | MIT | https://github.com/debug-js/debug |
| ms（debug传递依赖） | 2.1.3 | MIT | https://github.com/vercel/ms |

已核对实际安装包的package metadata与LICENSE；完整依赖闭包为rhea→debug→ms，没有Proton客户端绑定。`package-lock.json`只使用公开`registry.npmjs.org`的固定版本及SHA-512 integrity。安装使用`npm ci --ignore-scripts --no-audit --no-fund`，不执行包安装脚本；随后`verify_dependencies.js`核对固定依赖闭包及实际版本/许可，记录Node版本与锁文件SHA-256。Node.js使用CI的24系列并记录实际补丁版本，不能把这称作完全hermetic构建。该核对不替代未来安全审计或第三方再分发审查。

Windows构建会对固定的Proton0.40.0官方源应用可审计的OpenSSL/MSVC兼容补丁；脚本先核对受改文件SHA256，保留Apache头部并生成ECHOO_WINDOWS_OPENSSL_PATCH.txt变更说明，随候选一起分发。当前固定Windows构建打包要求该说明存在；缺失时失败，不把已修改的依赖当作未修改上游。

候选打包脚本要求携带项目LICENSE/NOTICE和完整Proton LICENSE/NOTICE，不把“链接了某库”视为许可核验完成；真实发布前仍需形成完整第三方清单与分发审查。安装 PostgreSQL/OpenSSL 的二进制发行包也需遵守其自身条款。

## 设计参考与实现来源

PostgreSQL 队列/可见性租约/代次确认等设计思想可以独立实现。首版没有直接复制 Kafgres 或 PGMQ 源代码。尤其 Kafgres 的 Elastic License 2.0 不能简单当作宽松许可证；若未来引入其代码，需单独审阅及保留通知。任何后续复制、衍生或新增依赖均需重新审阅，不能沿用本段作为自动许可。

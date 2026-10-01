# Echoo PGMQ

面向本地/门店 ERP 的 PostgreSQL 原生持久队列实现。扩展在 PostgreSQL 管理的 background worker 内直接监听 AMQP 1.0，协议编解码使用 Apache Qpid Proton；消息与领取状态存放在普通 WAL-logged PostgreSQL 表中。

**当前公开版本：v0.1.0，正式 Release；完整生产资格尚未完成。** [发布说明与隔离安装指南](docs/releases/v0.1.0.md)固定二进制源码ef12d354及普通CI36854708200，原始候选归档与blocked_security_review记录保留。本次为未签名分发。仓库包含可执行实现、自动化验证与候选打包流程。通过某个平台 CI 仅证明该次提交在该环境中的测试结果。Windows Server CI 不等于 Windows 11 实机验收；进程强杀不等于物理断电试验；真实 ERP 联调另行进行。项目采用标准Apache-2.0，署名与第三方范围见下方许可说明。

## 设计边界

- 一个 PostgreSQL 集群注册一个 listener，指向一个配置数据库；不需要独立 RabbitMQ 服务
- AMQP 1.0 over TLS，强制客户端证书；证书 CN 精确映射数据库角色与队列 ACL
- 完整 AMQP 消息编码保存在 bytea，保留二进制正文、属性、注解与关联标识
- 生产者的 Accepted 在数据库提交后返回；普通表、WAL、fsync 和同步提交是耐久性的基础
- 竞争消费、可见性租约、带 owner 与 generation 的确认，语义为至少一次
- SQL 入队可与 ERP 业务更新处于同一事务；AMQP 事务协调器不在首版范围
- 队列/全局数量与字节容量、消息大小、连接/link/inflight/缓冲区上限
- Accepted 删除、Released/Modified 重试、Rejected 保留死信；死信仍占容量

这不是 RabbitMQ 的通用替代品：不支持 AMQP 0-9-1、exchange/binding、topic 广播、AMQP 事务协调器、分布式 broker 集群和消息级 exactly-once 业务副作用。不要把 Kafka 协议、PGMQ JSONB 接口或 RabbitMQ 管理 API 套用到本扩展。

## 固定预览试用入口

以下是冻结的历史预览；新发行请使用[v0.1.0安装指南](docs/releases/v0.1.0.md)。.2包装器安装器固定旧450681d散列，不能安装本次ef12d354二进制，不要修改固定散列或覆盖旧ROOT。

PG18隔离测试修订包已到`preview-20260930.2`，扩展SQL仍0.1.0、原450681d二进制不变。[.2入门与双平台普通验证](docs/preview-20260930.2/README.md)修复新ROOT的SQL/AMQP完整编码限额错位，统一为65,536字节；原.1字节保留，使用全新目录试.2。TTL/priority/group仅透传，不执行对应broker语义；其它固定运行时边界见[版本化外部契约](docs/preview-20260930.1-contract.md)及.2变更说明。

本轮优化结论及后续优先级见[阶段总结](docs/performance-round-conclusion.md)，最新证据见[PROGRESS](docs/PROGRESS.md)。

## 从哪里开始

1. [快速开始](docs/quickstart.md)：构建、安装、角色/证书、AMQP 与 SQL 示例
2. [协议与兼容矩阵](docs/protocol.md)：地址、settlement、二进制与支持边界
3. [运维手册](docs/admin.md)：配置、监控、容量、备份恢复与故障处理
4. [存储设计](docs/storage.md)：表、事务、租约、幂等键与 ACL
5. [开发与验证](docs/development.md)：真实协议/升级/崩溃测试、CI 与候选产物
6. [升级与回退](docs/upgrades.md)：0.1.0 → 0.1.1 迁移演练与安全回退
7. [安全策略](SECURITY.md)、[已知限制与支持](docs/support.md)、[依赖许可证](docs/licenses.md)
8. [可复现 benchmark](docs/benchmark.md)：运行条件、原始结果与未验证项；不预填吞吐数字

## 最短验证路径

在安装了 PostgreSQL 18（主要目标）及16/17 开发文件、CMake、OpenSSL 和编译器的非 root 开发环境中：

```sh
python scripts/fetch_dependency.py proton /tmp/echoo-proton-src
cmake -S /tmp/echoo-proton-src -B /tmp/echoo-proton-build \
  -DCMAKE_INSTALL_PREFIX=/tmp/echoo-proton -DSSL_IMPL=openssl -DSASL_IMPL=none \
  -DBUILD_CPP=OFF -DBUILD_PYTHON=OFF -DBUILD_RUBY=OFF -DBUILD_GO=OFF \
  -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF -DBUILD_TOOLS=OFF
cmake --build /tmp/echoo-proton-build --parallel 2
cmake --install /tmp/echoo-proton-build
cmake -S . -B build -DPG_CONFIG=/usr/lib/postgresql/18/bin/pg_config -DPROTON_ROOT=/tmp/echoo-proton
cmake --build build --parallel 2
sudo cmake --install build
python -m pip install -r requirements-dev.txt
LD_LIBRARY_PATH=/tmp/echoo-proton/lib python scripts/run_integration.py --ordinary \
  --pg-config /usr/lib/postgresql/18/bin/pg_config --work-dir /tmp/echoo-test --keep
```

测试脚本会生成一次性 CA/证书，启动只监听 loopback 的临时 PostgreSQL，执行允许的普通 AMQP/TLS/SQL 回归，并正常停止集群。完整 qualification 与安全/故障测试当前等待审阅，不随普通 CI 执行；普通通过不等于完整验收。`--keep` 保留的目录含私钥和临时数据库，勿上传；CI 仅上传日志、JUnit 与环境信息。Windows 的原生构建步骤在 [快速开始](docs/quickstart.md) 与工作流中。

## 交付状态与许可

普通CI上传带SHA-256清单的candidate ZIP，不自动创建tag或Release。v0.1.0正式公开发行原样使用指定CI的六份ZIP，并附源码、原始证据、校验与来源记录；不宣称代码签名或生产认证。每个 PostgreSQL 主版本、操作系统与架构必须分别构建。请勿跨 PG 主版本复制扩展二进制。

本项目自行提供、未另行标注的代码与文档采用标准 [Apache License 2.0](LICENSE)，项目署名见 [NOTICE](NOTICE)：Copyright 2026 MixGeeker。第三方组件保留各自许可证、版权及NOTICE；本项目许可不替它们重新授权，详情见 [docs/licenses.md](docs/licenses.md)。

建议集成方在“关于”或“第三方声明”页展示“使用 Echoo PGMQ（MixGeeker），Apache-2.0”，并附项目链接；这是可选致谢，不是额外许可条件，也不替代适用的许可证/通知保留义务。Apache-2.0不强制使用某个关于页或展示位置。

新候选包携带项目LICENSE/NOTICE及依赖原始通知。已交付的preview-20260930.1/.2 ZIP、内层manifest及guide全部冻结；本次不悄悄重打同号包，历史包中pending-owner-decision是构建时记录。许可证选择和正式公开发行都不解除blocked_security_review资格状态。

## 验证与当前发布状态

v0.1.0二进制来自ef12d35447c7783f75ca123467208eb100533311，[普通CI36854708200](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36854708200)覆盖Ubuntu24.04/原生WindowsServer2022×PG18/17/16：源码与归档两轮共476次普通测试通过、12个core.sql完成标记。完整来源与未验证范围见[发布说明](docs/releases/v0.1.0.md)。整合包含原生扩展、七项SPI计划复用及.2包装器源码，排除SQL0.1.2实验；冻结包装器不能直接安装新二进制。固定450预览与后续ad589825重建各自的464次普通执行是独立历史证据，不混算。[验证记录](docs/validation.md)保留各次被测提交、checkout、归档和包装器来源。

历史恢复基线`37fc015`完成Linux/原生Windows × PG18/17/16六组合普通CI：416项、12个core.sql标记及全部归档散列通过；PG18每轮40项，PG16/17每轮32项。见[验证记录与候选下载](docs/validation.md)。

唤醒试验472e626因尚无稳定收益证据已撤回，完整[不利结果](docs/benchmark-wake-results.md)保留；其中PG-only自身不稳，不能把跨runner差异直接归因为代码。性能目标及完整安全/故障资格尚未通过；正式公开发行不表示已获得生产资格。

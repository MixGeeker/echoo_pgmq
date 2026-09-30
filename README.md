# Echoo PGMQ

面向本地/门店 ERP 的 PostgreSQL 原生持久队列候选实现。扩展在 PostgreSQL 管理的 background worker 内直接监听 AMQP 1.0，协议编解码使用 Apache Qpid Proton；消息与领取状态存放在普通 WAL-logged PostgreSQL 表中。

**当前版本：0.1.0 开发候选，尚非生产发布。** 仓库包含可执行实现、自动化验证与候选打包流程。通过某个平台 CI 仅证明该次提交在该环境中的测试结果。Windows Server CI 不等于 Windows 11 实机验收；进程强杀不等于物理断电试验；真实 ERP 联调另行进行。项目许可证仍待所有者决定。

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

在安装了 PostgreSQL 16/17 开发文件、CMake、OpenSSL 和编译器的非 root 开发环境中：

```sh
python scripts/fetch_dependency.py proton /tmp/echoo-proton-src
cmake -S /tmp/echoo-proton-src -B /tmp/echoo-proton-build \
  -DCMAKE_INSTALL_PREFIX=/tmp/echoo-proton -DSSL_IMPL=openssl -DSASL_IMPL=none \
  -DBUILD_CPP=OFF -DBUILD_PYTHON=OFF -DBUILD_RUBY=OFF -DBUILD_GO=OFF \
  -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF -DBUILD_TOOLS=OFF
cmake --build /tmp/echoo-proton-build --parallel 2
cmake --install /tmp/echoo-proton-build
cmake -S . -B build -DPG_CONFIG=/usr/lib/postgresql/17/bin/pg_config -DPROTON_ROOT=/tmp/echoo-proton
cmake --build build --parallel 2
sudo cmake --install build
python -m pip install -r requirements-dev.txt
LD_LIBRARY_PATH=/tmp/echoo-proton/lib python scripts/run_integration.py \
  --pg-config /usr/lib/postgresql/17/bin/pg_config --work-dir /tmp/echoo-test --keep
```

测试脚本会生成一次性 CA/证书，启动只监听 loopback 的临时 PostgreSQL，执行真实 AMQP/TLS/SQL 测试，并停止集群。`--keep` 保留的目录含私钥和临时数据库，勿上传；CI 仅上传日志、JUnit 与环境信息。Windows 的原生构建步骤在 [快速开始](docs/quickstart.md) 与工作流中。

## 交付状态与许可

CI 上传带 SHA-256 清单的 candidate ZIP，不创建 tag 或最终 Release，不宣称代码签名或生产认证。每个 PostgreSQL 主版本、操作系统与架构必须分别构建。请勿跨 PG 主版本复制扩展二进制。

项目尚未选定许可证，因此没有授予通用开源再分发许可。依赖自身许可证与归属说明见 [docs/licenses.md](docs/licenses.md)。本项目为独立实现，不包含 Kafgres/PGMQ 代码的直接复制。

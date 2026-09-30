# 独立客户端的普通AMQP互操作验证

本测试集使用Node.js 24上的 **rhea 3.0.5**，验证少量正常流量下的AMQP 1.0兼容性。rhea拥有独立的JavaScript编解码实现，不使用Python或C Proton客户端绑定；服务端仍使用Proton C 0.40.0。

这些用例不代表真实ERP联调、性能基准、任意客户端认证、安全资格、故障注入、持久链路恢复或自动网络恢复。新增场景不发送畸形帧、不制造容量耗尽、不强杀进程、不使用真实凭据，也不绕过证书验证。

## 执行普通验证

先按项目文档，为可丢弃的PG18安装构建并安装扩展，再从仓库根目录执行：

```sh
npm ci --prefix tests/interop --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org
node tests/interop/verify_dependencies.js
python scripts/run_integration.py --ordinary --independent-client \
  --pg-config /path/to/pg18/bin/pg_config --work-dir /tmp/echoo-rhea-run --keep
```

每次使用全新的工作目录。`--independent-client`必须与`--ordinary`一起使用，只追加3个明确列出的测试入口，参数展开后为8项：

- PG18普通CI：既有32项＋rhea 8项＝**每轮40项**；源码安装与候选归档重装各一轮
- PG16/17普通CI：不传新增选项，保持**每轮32项**；两轮计数与PG18分别记录
- 缺少Node、缺少依赖或协议错误均导致失败，不使用静默skip或xfail
- core.sql独立执行，必须输出完成标记，不能仅凭进程退出码判断通过

测试脚本在同一次调用和网络命名空间内启动PostgreSQL及Node，沿用现有的一次性mTLS证书生成方式，结束时正常fast关闭集群。`--keep`保留私钥和数据库文件，**不得上传整个工作目录**。CI只保留日志、JUnit、`environment.json`及不含私钥/消息正文的`independent-client.jsonl`；PG18两轮分别保留这些证据。

## 精确场景矩阵

| 用例数 | 地址 | AMQP参数 | 结果与独立证据 |
|---|---|---|---|
| 4项完整往返 | 精确普通名称与`erp/orders.store-17_<唯一后缀>` | 默认direct AMQP、默认sender；默认FIRST receiver或显式SECOND | 收到生产者Accepted；SQL中完整编码逐字节相等；接收二进制及metadata一致；消费者Accepted后队列计数和消息行清空 |
| 2项释放后重投 | 精确测试队列名称 | SASL ANONYMOUS；FIRST/SECOND均手动每次发1 credit | 释放一次，同一消息收到两次，随后Accepted并清空存储；SECOND还核对Released及Accepted的远端settlement |
| 2项正常关闭后重连 | 精确测试队列名称 | SASL EXTERNAL；FIRST/SECOND均手动每次发1 credit | 收到但不确认，完成AMQP Close握手，再建立新连接和link；租约到期后收到同一消息，Accepted并清空存储 |

### 生产与消费确认

每次发布都使用rhea普通sender默认设置，等待真实远端Accepted和settlement。rhea默认提议MIXED（2），服务端必须协商为UNSETTLED（0）；测试不使用预确认传输。

默认FIRST往返确实调用`open_receiver(address)`，保留autoaccept与锁定版本实现中的 **1000 credit默认窗口**。每个用例仅发布一条消息，不是资源上限测试。SECOND设置`rcv_settle_mode: 1`、`autoaccept: false`、`credit_window: 0`，显式授予1 credit，发送Accepted后等待匹配消息的远端settlement。释放、重连场景在两种确认模式下都采用手动单credit，不请求拓扑、selector、持久link、动态节点或特定broker的地址扩展。

### 消息保真与连接边界

所有连接设置`reconnect: false`。这里的重连明确建立新连接和新link，不是rhea自动恢复。正文包含0–255全部字节值，以及额外的零字节/0xff。断言逐项比较完整解码结果，包括UTF-8主题、message/correlation ID、内容类型/编码、durable/priority/TTL、路由/回复属性、时间戳、分组、应用属性和消息注解。任何receiver打开前，先通过SQL独立核对已存储的完整消息编码。

TLS使用现有一次性PKI的PEM CA、客户端证书和私钥，设置`servername: localhost`、`rejectUnauthorized: true`及最低TLS 1.2。Node内置CA验证与主机名验证保持开启。测试断言并记录实际授权状态、TLS版本和SASL协商结果；这不等于分别强制TLS 1.2、TLS 1.3的完整版本矩阵。

## 依赖来源与范围

`package-lock.json`固定完整依赖闭包：rhea 3.0.5（Apache-2.0）、debug 4.4.3（MIT）、ms 2.1.3（MIT），下载地址均为公开registry.npmjs.org，包含SHA-512 integrity。`npm ci`验证下载包，安装期间禁用包生命周期脚本及npm audit网络提交。

`verify_dependencies.js`核对预期依赖闭包、官方registry地址、实际安装版本/许可、rhea本地入口，记录Node版本与锁文件SHA-256。仓库不提交私有镜像地址或npm配置。这些开发测试依赖不随原生候选包分发，PostgreSQL服务无需Node.js。

主要参考：[rhea官方仓库](https://github.com/amqp/rhea)、[rhea npm包](https://www.npmjs.com/package/rhea)，以及安装后的锁定版本`lib/link.js`、`lib/connection.js`、`lib/message.js`、`lib/sasl.js`源码。

## 未来恢复完整qualification的准备要求

当前`.github/workflows/qualification.yml`与安全工作流均保持手动、暂停状态；本次不修改、触发或执行它们。新增测试文件属于默认`pytest tests`收集范围，因此未来恢复完整qualification时，Linux/Windows的**所有PG18/17/16完整任务**都会收集这8项，不能沿用只有Python依赖的旧环境。

正式恢复前必须先审阅并完成以下准备：

1. 为每个完整qualification任务安装Node.js 24，执行本页的锁定`npm ci`命令及`verify_dependencies.js`。只安装`requirements-dev.txt`不够，缺失依赖应在预检阶段明确失败
2. 完整qualification不应通过添加`--independent-client`选项绕过准备；该选项只允许与`--ordinary`组合，普通允许清单与默认全量收集是两条不同路径
3. 为全量路径补齐依赖预检、`environment.json`中的实际客户端信息、源码/候选两轮各自的`independent-client.jsonl`输出及artifact清单。当前这些自动记录仅由普通`--independent-client`路径启用
4. 重新审阅全量测试范围、授权及完整平台计数，之后才能解除暂停。不得通过删除用例、静默skip或把缺失环境当作通过来解决依赖缺口

这份准备要求不改变当前普通CI：PG18每轮40项，PG16/17每轮32项。

## 已验证范围

本地Linux PG18.6 / Node24.19.0已完成普通验证；fresh构建安装及候选校验重装后，两轮均40项通过且core.sql完成。472e626的Linux与原生Windows PG18 CI均已逐份核对：Node24.21.0/rhea3.0.5，两轮各40项及core.sql通过，包含每轮16条独立客户端JSONL证据。被测checkout、归档与支持边界见[普通验证记录](../../docs/validation.md)。

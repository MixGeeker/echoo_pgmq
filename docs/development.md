# 开发、测试与候选打包

## 源码布局

- `src/`、`include/`：PostgreSQL生命周期、原生socket事件循环、Proton协议状态与事务桥
- `sql/`：0.1.0基础、0.1.1加法迁移/全新安装
- `tests/test_amqp_integration.py`：真实TLS/AMQP1.0二进制与metadata roundtrip、commit-before-Accepted、重投/过期ACK、拒绝与资源边界
- `tests/test_storage_integration.py`、`tests/sql/`：并发领取/配额/幂等、权限、SQL业务回滚、升级失败恢复、pg_dump恢复
- `tests/test_z_crash_recovery.py`：强杀worker、immediate停止/启动PG、已提交/未提交消息区别
- `scripts/run_integration.py`：隔离集群启动、一次性证书、运行测试与停止；基础设施缺失报错，不静默skip
- `scripts/package_candidate.py`：候选ZIP与文件散列清单；不发布Release
- `bench/`：单独的可复现实测工具，参见 [benchmark说明](benchmark.md)

## 可复现运行

构建依赖为PG18（主要目标）及16/17开发安装、Proton0.40.0(OpenSSL)、CMake/C11编译器。运行示例见快速开始。当前允许的普通回归使用固定清单：

```sh
python scripts/run_integration.py --ordinary --pg-config /path/to/pg_config --work-dir /tmp/echoo-one --keep
```

每次使用全新的work-dir。脚本只对自己创建的cluster设置loopback trust，并在pg_hba最前拒绝worker角色的外部连接；不是生产认证配置。Python集成测试只有被该脚本注入的DSN/证书路径，不猜测默认本机数据库。Linux不可root运行initdb；Windows使用原生Python/PG/VisualStudio而不是WSL。

私钥/PGDATA绝不作为CI artifact上传。日志/JUnit/environment.json用于证据，失败不能只截取最后一句。多个PG主版本不可共用同一个build目录或同一二进制安装路径。

## CI策略

GitHub Actions在PR与main推送上运行，六个功能矩阵：Ubuntu24.04 PG18（主要目标）及16/17、WindowsServer2022原生PG18（主要目标）及16/17。每个自动任务安装扩展，执行 `--ordinary` 明确允许的 SQL/storage 并发、备份/升级、候选归档单元测试，以及正常 AMQP 二进制/提交/重投/迟到 ACK/release 语义，PG18额外安装锁定的Node/rhea测试依赖并运行8项独立实现互操作，合计每轮40项；PG16/17每轮32项。源码安装和归档重装均成功后保留candidate证据。自动任务不包含畸形输入、连接耗尽、进程强杀或故障注入。actions固定commit SHA，权限只读，checkout不保留凭据，没有publish-release、token写权限、continue-on-error或伪造成功。

完整功能矩阵保留在 `qualification.yml`，安全矩阵保留在 `security.yml`，均仅允许手动 `workflow_dispatch`，当前等待安全审阅，不自动触发。所有 candidate manifest 均明确写入 `qualification_status=blocked_security_review`。普通任务通过不等于完整验收，未执行的测试不会被记作通过。

Proton官方源包固定SHA512；Windows官方EDB ZIP固定SHA256与版本。OS软件包通过签名仓库，runner与传递依赖仍有变化，因此这不是字节完全可复现的hermetic构建。候选同时保存PG编译选项和Python环境清单，审阅依赖更新后再更换固定值。

Server2022的原生测试能发现MSVC/WinSock/DLL/SChannel客户端差异，不能替代Windows11工作站的服务身份、NTFS权限、防病毒/防火墙、真实磁盘和断电验收。

## 结果解释

- `pass`只能描述实际运行的测试；未运行平台、真实掉电、ERP业务联调和生产负载必须明确标未验证
- 故障注入范围是进程终止、拒绝输入与事务中人为异常；并非全面故障模型
- 真实AMQP tests利用客户端协议roundtrip刷新settlement，再独立查询数据库；不能因客户端本地队列清空就假定ACK已持久化
- 不用所有Exception都算“成功拒绝”的模糊断言；TLS/transport拒绝仍需核对消息没入库与worker存活
- 固定配额测试要证明边界内恰有N次成功，其余得到预期SQLSTATE，不把连接失败当容量正确

## 候选产物

打包前将上游Proton LICENSE/NOTICE放到PROTON_ROOT下可找到的受控位置：

```sh
python scripts/package_candidate.py --pg-config /path/to/pg_config --build-dir build \
  --proton-root /path/to/proton --output dist
```

ZIP含扩展、版本SQL/control、Proton运行库、中文文档、第三方许可、MANIFEST与SHA256旁文件。WindowsOpenSSL由可信发行版/系统提供，不覆盖PG已有DLL。打包拒绝CMake开启故障注入的构建，并扫描已安装库中的专用hook标识，避免测试库混入候选。该包没有自动签名、最终发布或无审核自动部署；项目许可仍待所有者决定。候选安装/ABI核查参见quickstart。

## 发布门槛（尚需逐项形成证据）

完整平台CI通过；Windows11原生实机；长时间负载/磁盘满/容量与恢复；安全审阅/依赖审计；消息语义和ERP幂等联调；真实掉电与备份恢复；明确许可、发布签名、升级/回退手册及支持责任。不得用一项通过替代其它项目。

## 从候选归档重新安装验证

普通 CI 在初次普通回归和打包之后，再核对 ZIP 旁文件 SHA256、MANIFEST 中的完整文件集合与每文件散列。`scripts/install_candidate.py` 拒绝路径穿越、重复成员、未列出的文件及主版本/架构不匹配。该脚本只用于全部集群已停止的可丢弃 PostgreSQL 安装，需要显式传入 `--disposable-installation`；它会替换该安装中的 Echoo/Proton 文件，不是生产安装器或升级工具。

Linux 从候选复制扩展、SQL 与 Proton 库，随后去掉 `LD_LIBRARY_PATH` 和 `PROTON_ROOT`。Windows 将候选 Proton DLL 放到测试 PostgreSQL 的 bin，去掉构建 Proton/OpenSSL bin 路径，保留 PostgreSQL 发行版自带的 OpenSSL。之后从新的数据目录重新执行同一普通回归清单，包括正常启停、SQL、AMQP 和升级，不执行进程故障测试。两轮日志与 JUnit 都保存为证据。该步骤旨在避免构建机 PATH/RPATH 或已安装旧文件掩盖不完整归档；Windows 服务身份与实机环境仍需单独验收。

SHA256 用于发现文件损坏和清单不一致，不代表签名认证；不可信来源能够同时替换 ZIP 与散列。正式发布仍需独立可信分发与签名流程。

当前已核实普通候选对应472e626：PG18 Linux/原生Windows两轮各40项（含独立rhea 8项），PG16/17各两轮32项，core.sql显式完成记录齐全，见[验证记录](validation.md)。文档后续变更不自动变更该被测提交或完整安全资格状态。

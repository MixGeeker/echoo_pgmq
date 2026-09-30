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

构建依赖为PG18（主要目标）及16/17开发安装、Proton0.40.0(OpenSSL)、CMake/C11编译器。运行示例见快速开始。直接测试某一用例：

```sh
python scripts/run_integration.py --pg-config /path/to/pg_config --work-dir /tmp/echoo-one --keep -- \
  tests/test_amqp_integration.py -vv -x
```

每次使用全新的work-dir。脚本只对自己创建的cluster设置loopback trust，并在pg_hba最前拒绝worker角色的外部连接；不是生产认证配置。Python集成测试只有被该脚本注入的DSN/证书路径，不猜测默认本机数据库。Linux不可root运行initdb；Windows使用原生Python/PG/VisualStudio而不是WSL。

私钥/PGDATA绝不作为CI artifact上传。日志/JUnit/environment.json用于证据，失败不能只截取最后一句。多个PG主版本不可共用同一个build目录或同一二进制安装路径。

## CI策略

GitHub Actions在PR与main推送上运行，六个功能矩阵：Ubuntu24.04 PG18（主要目标）及16/17、WindowsServer2022原生PG18（主要目标）及16/17。每个任务安装扩展、运行实际SQL/AMQP/恢复测试，成功后才打包candidate。actions固定commit SHA，权限只读，checkout不保留凭据，没有publish-release、token写权限、continue-on-error或伪造成功。

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

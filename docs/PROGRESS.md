# 开发与验收进度

最后核实：2026-09-30 03:25 UTC。草稿 PR 正在推进，尚未达到生产发布标准。

## 主目标

PostgreSQL **18** 是优先支持和验收版本，Linux 与原生 Windows 都必须通过。PostgreSQL16/17为补充兼容矩阵。部署形态为 PostgreSQL 原生扩展及受管理后台 worker 直接提供 AMQP1.0，不依赖额外broker、sidecar或Docker。

## 已有实际证据

- PostgreSQL16.15、17.11、18.6原生扩展均已在Linux编译
- 首轮PG17.11与PG18.6当时的38项功能测试通过；最终普通候选152a73c的六组合两轮32项与core.sql也已核实，均非模拟协议服务器
- 已验证mTLS连接、二进制及元数据逐字节保真、发布提交后Accepted、手动ACK、旧代次与过期ACK拒绝、断连重投、重试和死信
- 已验证并发领取、并发幂等与配额竞态、跨角色权限拒绝、DROP/重建角色不继承旧授权、事务回滚计数
- 已验证真实扩展安装、0.1.0→0.1.1及失败事务回滚、逻辑备份恢复正文/ACL/租约/序列
- 120次未授权/无证书/未知队列尝试后worker PID不变；SIGKILL后已Accepted消息恢复，immediate restart保留已提交并回滚未提交
- RabbitMQ4.0.5与echoo真实AMQP1.0基准脚本100条冒烟均通过；这不是正式性能成绩

## 当前正在做

| 项目 | 当前状态 |
|---|---|
| PG18主验收 | 152a73c：Linux与原生Windows PG18两轮各32项及core.sql通过；完整安全/故障资格仍未通过 |
| GitHub六项原生矩阵 | 六组合普通矩阵全部通过并逐份核对证据；完整安全/故障资格矩阵保留手动但不触发 |
| Windows首次CI问题 | Proton/MSVC、私有暂存ACL、SChannel客户端和ZIP名称兼容均已修复；真实受限子进程启动及两轮普通测试通过 |
| 提交边界异常测试 | settle成功标志已移到commit之后；默认编译关闭的故障注入1项已在PG17通过；新的故障注入工作暂停 |
| 反向锁序 | poison隔离不再取得配额锁，queue计数采用NO KEY UPDATE允许FK KEY SHARE；明确并发回归已通过 |
| 安全证据 | d24d57a自动GCC、ASAN/UBSAN fuzz、Python依赖审计通过；人工审查不完整，存在未验证的资源上限静态候选；相关调查暂停，安全发布闸门未过 |
| 对比性能 | PG18与RabbitMQ三轮各5000条探索性冒烟完成，原始数据保留；吞吐相近、echoo端到端p95更高，合成DB延迟增幅未达≤10%候选目标 |
| 候选包 | 六组合均完成候选校验、正常停止后归档重装、去掉构建库路径后的32项+core.sql；运行中DLL占用及故障升级资格仍未验收 |

## 开发中发现并修复

1. 受拒绝link重复释放导致原生SIGSEGV，现已修复并通过重复拒绝压力回归
2. Proton解码器宽松接受非法message，现增加完整typed-section验证；不重编码合法原始消息
3. 空/过大/非法SQL消息曾回滚领取、永久阻塞队首；现有界扫描并在同一事务隔离为保留死信
4. settle异常路径可能在commit失败后返回成功，已修复；暂停前PG17独立注入1项通过，最终完整故障资格仍未验收
5. 逻辑恢复计数singleton冲突、身份序列遗漏，已修复并验证；恢复时需显式安装源扩展版本，不能依赖目标默认版本
6. PG16无后台NOLOGIN绕过API，使用无密码专用LOGIN角色并以pg_hba拒绝外部连接；PG17/18采用NOLOGIN

## 必须公开的边界

- 至少一次，不承诺端到端exactly-once；AMQP0-9-1、复杂exchange、broker事务和跨节点复制不在首版范围
- 普通WAL表、同步提交；全局容量锁存在跨队列竞争，长ERP事务需单独评估
- GitHub x64Windows为Server，不能替代门店Win11 x64、4核8GB SSD验收
- 进程强杀不等于物理断电；真实电源/SSD资格测试未运行
- 真实ERP联调按维护者安排后续共同进行，合成库存事务不代表ERP
- 项目许可证等待维护者决定；不擅自选择许可或签名密钥
- 当前不合并、不发布正式版本、不生产部署

## 当前自动与暂停闸门

自动CI仅执行普通编译、安装、业务SQL并发、正常AMQP收发及归档包安装。完整qualification与security工作流保留为手动但本阶段不触发；测试和断言均保留，没有把减少后的普通套件当作完整验收。所有候选manifest标记qualification_status=blocked_security_review。

## 已完成的普通兼容里程碑

`152a73c` 六个Linux/原生Windows × PG18/17/16组合均完成两轮32项普通回归与显式core.sql；归档重装、移除构建依赖路径后再次通过。已逐份下载核对12组JUnit/environment证据。Windows PG16曾漏执行core.sql的harness问题已修复并重测。候选仍为blocked_security_review，详情与归档见 [验证记录](validation.md)。

固定预算基准首轮e2f71c9因HTTP采样客户端实现问题在预热前停止，测量样本为0，失败数据已保留且不作性能结论。修复后锁定51ca717；真实Docker前置smoke已3/3通过：每单元5秒、10个HTTP资源样本、实际2CPU/4GiB/0swap；两代理各700条含暖机对账完整且最终队列为0。27单元完整固定时窗矩阵已启动，结果仍待终态。见[smoke证据](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36663717022/artifacts/11075865839)和[当前运行](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36663717022)。本阶段不会触发被暂停的安全/完整故障工作流。

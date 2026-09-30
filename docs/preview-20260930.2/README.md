# preview-20260930.2：SQL与AMQP限额一致的隔离测试套件

本次只更新预览包装器与普通演示，内层候选ZIP、固定450681d源码、extension SQL0.1.0均不变。旧.1已发字节保留；使用新独立ROOT，.2不会把旧.1ROOT当可升级目标。

## 线上使用补充（2026-09-30 UTC）

本节在.2套件交付后补充，仅更新这个版本化网页。已发.2 ZIP及其guide文件、SHA-256和二进制均冻结；下载包不包含本节，后续新版本才会纳入。

**SQL入队所在的业务事务应保持短。** 严格全局容量记账会持有共享容量行锁直到外层事务提交或回滚，因此一条队列的长事务也可能拖延其他队列的发布与消费确认。业务逻辑允许时，先完成必要的业务行加锁和修改，在事务末尾调用现有enqueue/enqueue_binary，然后立即COMMIT；业务数据与消息仍须在同一事务原子提交，容量不足时一并回滚。

所有相关调用点和业务触发器应保持一致的锁顺序，建议先业务行、后队列容量/消息。避免一条路径先入队再等待业务行，而另一条路径反向获取锁；仅移动一个调用不能保证消除死锁。如果后续业务需要返回的message ID，剩余事务也应尽量短。

不要在持有这些锁的事务中等待外部网络I/O、用户操作或其他非数据库工作。可提前完成不依赖最终事务状态的准备工作，减少持锁期间的应用往返；不要无界扩大批次或只调高锁超时来掩盖持续争用。[跨队列等待的实测与使用建议](https://github.com/MixGeeker/echoo_pgmq/blob/438e8404b6ec54b01419bebf8d4808d664fd211f/docs/performance-global-contention.md)解释了锁持有范围和证据边界。这些是使用约束，尚未在真实ERP联测，也不新增性能承诺。

## 附带普通六组合CI已收齐（2026-09-30 21:16 UTC）

[CI36776023129](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36776023129)最终六组合成功；[独立逐份核验](ordinary-matrix-36776023129.json)覆盖12个外层artifact、6个内层候选的全部manifest/hash、12份JUnit/environment与6份job日志：464次普通测试，0失败/错误/跳过，12个core.sql标记。PG18每平台每轮44项，PG16/17每平台每轮36项。Linux16/18软件包安装一度较慢，随后正常完成，无需改仓库源或重跑该任务。

此重建使用actual checkout ad589825，资格仍blocked_security_review；不替换已交付.2内的原450二进制。此前普通run36775187167与36775523789因后续修复提交按既有concurrency规则取消，其已通过job和取消状态保留，不合并计作当前通过次数。Windows包装器的两次预检查失败仍见下方原记录。

## 明确的行为变化

.1的worker限额65,536而SQL默认1MiB，合法SQL消息可能先成功入队再被worker因尺寸转dead。.2在新库初始化时INSERT全局限额65536（扩展刚装好时该表为空），同一常量设置worker GUC和包装器队列限额。SQL admission为min(global,queue)，允许管理员进一步收紧；直接create_queue默认1MiB也受全局65536约束。

计量对象相同：enqueue_binary先message_binary/Proton编码，然后_enqueue用octet_length(final bytea)，AMQP发布原样保存完整encoded bytes，消费以同一body长度检查。不是应用payload的64KiB额度，也不估算固定metadata开销。raw enqueue仍不校验编码合法性；本修复不扩展这个专家接口的合同。TTL/priority/group仅透传、无对应broker语义及逐消息提示，完整说明见[入门指南](guide/README.zh-CN.md)。

start与两个demo检查实际GUC/SQL一致；policy-check导出无凭据的实际限制与计数。新增12字节合法SQL→AMQP用例，独立rhea验证二进制envelope并等待远端SECOND结算。保留原3个AMQP场景和SQL事务demo。

## 普通验证状态

- 固定二进制继承CI36720731289的464次普通执行；不是本次新增测试数
- Linux Debian13.6/PG18.6原450归档七阶段通过，实际worker/global/5队列均65536、最后计数与保留行归零，正常stop/status3及同执行会话进程/监听端口退出，4.81秒
- 首次Linux后处理误把12字节写为11，并以bind误判TIME_WAIT，失败记录保留；改正控制器后重做普通烟测，运行时字节未变
- .2离线配置/严格文件散列/端口选择/结果判定共12项，本地与Windows通过；不发送边界消息
- Windows Server2022/PG18.6新包装器[CI36776023123](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36776023123)七阶段通过；原归档、12guide文件、控制器与固定输入散列均独立核验。3个原场景和12字节SQL→AMQP的远端SECOND结算、5队列归零、实际编码限额与正常stop/status3均有原始记录
- 测试head fd30a1f，实际checkout ad5898250bed211bde6709c1306238bb2d4b07f5；整树多了18项基础分支报告与未安装诊断patch，运行时/guide/工作流无差异，不能称整树相等
- Windows最初两轮分别在字节门禁（LF→CRLF）与固定默认端口绑定（WinError10013）失败，均未启动PG；已保留原件。仅限定新guide eol=lf、CI用OS分配的loopback端口，未改变用户默认端口/系统保留配置或放宽散列验证
- 完整结论、失败历史及两平台套件hash见[verification-final.json](verification-final.json)。guide/VALIDATION.json保留生成guide时的准备快照；最终平台证明以本追加记录为准

完整安全/故障资格、Win11设备/真实SSD/ERP/生产SLA未通过；状态仍blocked_security_review。无新性能承诺、不合并main、不创建正式生产Release。已发.1仍可按原指南正常stop保留；.2作为新套件使用全新ROOT，原.1与原内层二进制字节均保留。

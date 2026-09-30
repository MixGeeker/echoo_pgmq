# preview-20260930.2：SQL与AMQP限额一致的隔离测试套件

本次只更新预览包装器与普通演示，内层候选ZIP、固定450681d源码、extension SQL0.1.0均不变。旧.1已发字节保留；使用新独立ROOT，.2不会把旧.1ROOT当可升级目标。

## 明确的行为变化

.1的worker限额65,536而SQL默认1MiB，合法SQL消息可能先成功入队再被worker因尺寸转dead。.2在新库初始化时INSERT全局限额65536（扩展刚装好时该表为空），同一常量设置worker GUC和包装器队列限额。SQL admission为min(global,queue)，允许管理员进一步收紧；直接create_queue默认1MiB也受全局65536约束。

计量对象相同：enqueue_binary先message_binary/Proton编码，然后_enqueue用octet_length(final bytea)，AMQP发布原样保存完整encoded bytes，消费以同一body长度检查。不是应用payload的64KiB额度，也不估算固定metadata开销。raw enqueue仍不校验编码合法性；本修复不扩展这个专家接口的合同。TTL/priority/group仅透传、无对应broker语义及逐消息提示，完整说明见[入门指南](guide/README.zh-CN.md)。

start与两个demo检查实际GUC/SQL一致；policy-check导出无凭据的实际限制与计数。新增12字节合法SQL→AMQP用例，独立rhea验证二进制envelope并等待远端SECOND结算。保留原3个AMQP场景和SQL事务demo。

## 普通验证状态

- 固定二进制继承CI36720731289的464次普通执行；不是本次新增测试数
- Linux Debian13.6/PG18.6原450归档七阶段通过，实际worker/global/5队列均65536、最后计数与保留行归零，正常stop/status3及同执行会话进程/监听端口退出，4.81秒
- 首次Linux后处理误把12字节写为11，并以bind误判TIME_WAIT，失败记录保留；改正控制器后重做普通烟测，运行时字节未变
- .2离线配置及证据校验由本目录test_*.py执行；只验证生成路径/结果判定，没有启动服务或发送边界消息
- Windows Server2022/PG18.6新包装器七阶段CI待结果；不能借用.1通过记录宣称.2通过

完整安全/故障资格、Win11设备/真实SSD/ERP/生产SLA未通过；状态仍blocked_security_review。无新性能承诺、不合并main、不创建正式生产Release。已发.1仍可按原指南正常stop保留；.2资格就绪后才另发新套件。

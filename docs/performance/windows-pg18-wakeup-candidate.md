# Windows PG18 测试候选：身份与验收边界

仅用于隔离、可丢弃的测试实例；不是新的正式 Release，也不要直接覆盖生产实例。
当前仍是 Draft PR #12，main 与 v0.1.0 Release 资源均未改变。

## 获取与核对

来源：[普通回归 run36913163692](https://github.com/MixGeeker/echoo_pgmq/actions/runs/36913163692)

- Actions artifact：`native-windows-server2022-pg18-candidate`
- Artifact ID：`11188316676`
- Actions 外层 ZIP SHA256：`d180e907773a191eb5b0013bfa65f16822d31765cbe4a131f97d41efd1329357`
- 内层安装包：`echoo-pgmq-0.1.0-candidate-pg18-windows-amd64.zip`
- 内层 ZIP：731,974 bytes
- 内层 ZIP SHA256：`8a1fbe48127e83e2b0477484517b03b082c02765fa434ad96dc99512cf01b1ae`
- 包内 `lib/echoo_pgmq.dll` SHA256：`2ef697ce337b44f8a035df9b482d7e3829c6278f965c5600ada4e1f99149e738`
- 当前 Actions 到期时间：2026-10-15 19:21:20 UTC；此链接不是永久归档

已核对外层 artifact 摘要、内层 ZIP 的 `.sha256`、包验证器，以及 MANIFEST
列出的全部103个文件摘要。没有执行包内程序或对实际用户电脑安装。

包名沿用候选构建版本0.1.0，不能仅靠文件名区分已发布的 v0.1.0。
请以这份摘要与来源记录区分，不要重命名或覆盖原 Release 资源。

## 源码来源

- PR server head：`82adc0d0712c10cb3450e1ad6c594ede18a517e5`
- 包内 MANIFEST commit：`4aa6c9bf9b6bed4157fe2fda9d9129a064b0e401`
- 后者是 CI 的 PR 合并测试提交，不是已经合入 main；其父提交为 main115dcfe 与 head82adc0d
- 二者源码树一致：`060bbfb225e83aa2bcaeb30c30bb3d0fd8a3e002`
- 性能实验的已修正 harness：`8a1347c6f0826a29ed2aa59d60328b9c3f267d23`；服务端 C/SQL/build 输入与82adc0d相同

## 已验证与尚未验证

已通过 Windows Server2022 / Linux 的 PG16、17、18 普通回归和候选归档重装；
该安装包为 Windows AMD64、PostgreSQL18.6 构建，Proton0.40.0。
需要匹配的 PostgreSQL 主版本与架构、受信任来源的 OpenSSL3.x 及微软运行库。
包不捆绑或覆盖 OpenSSL DLL，也不要求服务运行时安装测试 Python 环境。

尚未建立 Windows11、实际 ERP 联测、店端存储、物理断电或完整安全验收结论。
MANIFEST 的 qualification_status 仍为 `blocked_security_review`，包未签名。
正确性 CI 通过不能替代这些验收。

## 本轮性能结论

默认50ms polling、持久化开启。稀疏收消息延迟明显改善，但 Windows W32 的
等量消息 worker CPU 开销增加；持续 backlog 吞吐没有普遍提升，Linux 有一轮
首批153ms P95保留在结果中。详见[已核对的四组实验报告](queue-wakeup-factorial-results-8a1347c6.md)。

如进入实际 Win11 联测，应保留原安装及回退副本，只在独立测试实例按照
`docs/quickstart.md`、`docs/admin.md` 与包内 `INSTALL-CANDIDATE.txt` 核对依赖、
ABI、路径、ACL和摘要。不要同时改 polling、系统计时器或关闭持久化，否则
无法区分这次服务端改动的效果。是否进入联测、替换或正式发布应另行确认。

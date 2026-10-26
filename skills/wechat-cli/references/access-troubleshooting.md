# 接入失败怎么处理

对外反馈优先`access.sh diagnose --support-summary`（2026-09-22.2+），不要要求用户发送完整日志。网页版没有本地工具、其他项目已跑通、Windows目录权限和材料复用见[接入支持流程](../../../docs/access-support.md)。安装后的独立Skill无法打开该仓库链接时，参考公开仓库同名文档。

2026-09-22起优先使用`access.sh diagnose`；需要检查用户已有材料时加`--source <本机文件> --database-root <本人数据库目录>`。材料在临时配置中验证，主配置保持不变。返回`material_error.code`定位JSON、raw key、salt、SQLCipher等问题。UTF-8 BOM、UTF-16/32和严格的`x'…'`字面量已兼容；图片/API密钥和口令不作为raw key。

`unrecognized_readable_count`表示已经解密但表结构尚未识别的数据库，不要要求用户重新获取key。`verification_level=sqlite_schema_read`不是所有页面完整性或所有聊天覆盖的证明。

`diagnose --jev-request`可生成仅含固定状态的JEV分类请求，不联网，不包含私人路径或聊天；外部建议不得改变本机验证状态或自动触发获取。模型不可用时直接使用确定性诊断。

Windows 的平台入口、DLL候选转换、SQLCipher、ACL与文本验收见 [Windows 分阶段指引](windows-access.md)。Reader已加入真实ACL代码，但尚未Windows真机验收；自动获取仍未合入。

适用于本人电脑和本人账号。Codex负责检查、材料转换和配置，用户负责微信登录、账号选择与系统授权。不要让用户手工复制key，也不要把每个错误都归因于微信版本。

## 统一诊断入口

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/wechat-cli/scripts/access.sh" status
```

只检查本机，不启动获取、不重启微信、不解除恢复锁。输出系统/微信版本、数据库计数、Reader状态、上次失败阶段和固定诊断提示。存在恢复锁时，还会检查进程可执行文件名，仅输出官方微信、副本和已知获取工具的计数，不输出命令行参数、PID、路径、聊天或key。可以让用户先检查该JSON再提交Issue；不要索取完整配置、密钥或原始日志。

`environment.access_helper_revision`用于区分助手代码修订；Reader版本相同不代表接入助手也已经更新。若没有`status`子命令，先核对实际运行路径，再保留配置升级代码，不能改用老的获取工具补齐。

`status`退出码0表示诊断命令完成，不等于接入成功；必须检查`live_database_read_ok`和`state`。`last_attempt`是历史尝试，不表示当前仍在失败或仍有获取进程运行。

| 状态或信号 | 应做的检查 | 不应做的事 |
|---|---|---|
| `ready` | 抽检群聊、私聊、标签和时间范围，直接使用 | 重取key |
| `existing_provider_material_available` | 仅表示固定清单里某个已知工具的输出文件存在（wxkey、wechat-cli、wcdb-key-tool），见`material_candidates`。确认是本账号材料后，对`importable:true`的文件用`onboard --source`显式验证；口令格式先保留 | 当成已验证key，或无视它再获取 |
| `dependency_required` | 检查实际CLI解释器的SQLCipher/zstandard；使用安装脚本提供的独立环境 | 以为系统Python装了包就一定可用 |
| `account_selection_required` | 请用户确认唯一目标账号 | 根据目录修改时间猜账号 |
| `database_read_permission_denied` | 核对实际提权Python的文件访问权限。root不自动豁免TCC | 反复重启微信、全盘搜索或关闭SIP |
| `debugger_unavailable` | 修复Apple Python与LLDB/Command Line Tools。助手在provider前验证导入 | 认为Reader自检通过就代表调试器也可用 |
| `debugger_api_incompatible` | LLDB可导入但缺少关键启动身份/事件接口；先修复工具链兼容性，provider尚未启动 | 重启微信或反复授权解决API缺失 |
| `verification_failed`且用户提供了材料 | 用`onboard --source ...`进行临时转换验证；相对路径、schema-2和Reader salt_keys并非同一格式 | 直接丢弃旧材料、再次获取 |
| `account_salt_mismatch` | 检查登录账号和目标数据库是否一致 | 跨账号合并key |
| `target_launch_failed` | 核对副本启动策略、实际进程UID，参考Issue #3 | 把HOME/USER环境变量当作已降权证明 |
| `no_derivation_observed` | 核对登录、进程身份与版本兼容性；这是提示，不是根因证明 | 不看阶段就无限延长等待 |
| `partial_key_coverage` | 验证已生成材料和缺失范围 | 称为全量历史或立即重新获取 |
| `previous_run_requires_review` | 先`status`，核对获取进程结束和官方微信恢复，再由用户决定下一步 | 自动删锁、隐藏重试 |

## 完成恢复检查

获取后的`state:ready`只表示数据库验证通过，恢复锁不会因此自动移除。保留锁不影响正常查询和日报，只阻止新的获取尝试。

`recovery_check`只说明当前观察到的进程，无法验证微信是否登录，也无法识别所有可能改名的获取进程或尚未完成的授权任务。`login_verified`和`all_acquisition_processes_verified_stopped`因此保持false，不能仅看计数为0就断言全部恢复。

先确认官方微信可以正常使用，检查本次获取进程和授权窗口已结束。**用户事后明确确认两项事实后**，Codex才可执行：

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/wechat-cli/scripts/access.sh" finish-recovery \
  --confirm-wechat-ready --confirm-no-acquisition
```

命令会重新检查当前进程；检查失败、有副本/其他微信实例/已知获取工具、官方微信未运行、锁不安全或已变化时，都保留恢复锁。不提权、不杀进程、不重启微信，不删配置或材料。两个参数不是自动确认按钮，不能从最初的获取授权或一句“继续”推断事后确认。检查有疑问就停止，不改用`rm`绕过。

## 目录与材料

- 获取助手接受单一账号目录、该账号的`db_storage`，或只含一个账号的数据父目录；传给wxkey之前统一成账号目录。多个账号仍要求用户选择。
- 文件头预检只检查这个账号的`db_storage`，不把媒体目录、其他账号或无关缓存纳入权限验证。
- 普通导出目录可以供Reader读取，但不是wxkey的账号目录，不能交给获取工具碰运气。
- 相对路径材料在临时私有目录归一化后验证；Reader已导入的`salt_keys`也能再次复用。96位raw key的后32位必须匹配其salt。
- `onboard`预览不发布配置；`--apply`才会写入。已有配置不覆盖，转换失败不自动改走获取。

## 兼容性与边界

仓库Issue #2、#3及#75包含不同macOS/微信版本上的失败报告，不能用单一版本号判断成功率。当前仍没有新机器端到端成功率统计。目录适配和材料导入通过虚构SQLCipher数据库测试，不代表所有版本的取key已跑通。

固定wxkey候选已附启动身份、异步等待和清理补丁，但真实提权获取、Apple启动策略及失败后的官方微信恢复仍需新机器验证；没有关闭系统防护。Windows的已有材料读取与Windows自动获取是两件事，后者尚未实现。数据库key也不等于图片/视频/语音解密能力。

后续验收必须记录：系统与微信完整版本、CPU架构、Reader/helper版本、provider固定提交和构建哈希、失败阶段、数据库覆盖计数、官方微信是否恢复。以同一台机器的一次首次接入为分母统计成功率，不能把多次重试或作者已有key的读取算成新机成功。

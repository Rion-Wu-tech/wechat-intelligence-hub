# 首次接入流程

Windows 用户先读 [Windows 分阶段接入指引](windows-access.md)。社区已有特定版本成功案例，但获取分支尚未合入；不能把 Mac 获取命令反复用于 Windows，也不能把社区附件当成自动执行授权。

Codex负责执行检查、准备工具、获取材料、验证和配置。用户不需要寻找或复制key，只需登录自己的微信，并在需要时确认影响、完成系统授权。安装和日常读取不执行获取工具；首次获取属于单独确认的实验性路线。

## 用户怎么调用

> 用 $wechat-cli 帮我接入这台电脑上我自己的微信。已有配置或key就复用；没有就帮我准备适配工具，说明影响并确认后获取。完成验证和配置，不要让我复制key或手工拼命令。

不增加第三个Skill。`wechat-cli`负责接入和只读证据，`wechat-intelligence-hub`在可读后负责个人Profile、检索分析和报告。

## Codex执行的五步

1. **环境与账号。** 执行 `reader.sh self-test` 和 `access.sh onboard`；检查本机微信版本、数据目录与读取依赖。多账号让用户选择，没有登录让用户登录；不要索要微信密码或扫码登录凭据。
2. **复用或准备工具。** `ready`就复用；已有材料显式传 `--source`。`existing_provider_material_available`表示只检测到固定清单中某个已知工具的输出文件存在（`~/.config/wxcli/config.json`、`~/.wechat-cli/all_keys.json`、`~/.wcdb-key-tool/wechat-passphrase.json`），尚未读取或确认可用；`material_candidates`给出工具名和能否直接导入；确认来源和账号后，Codex用`onboard --source`验证，不能把内容复制进对话。确实缺材料时，由Codex核验并准备[实验性获取工具](experimental-access.md#由codex准备工具)，不要让用户自己网上找key，也不要搜索无关应用的秘密文件。
3. **确认后获取。** 把进程访问、退出/重启微信和重签名副本的影响用一句话讲清。确认后Codex运行同一入口；管理员密码只由用户填进系统授权框，Agent不得代填、截图读取或从Keychain提取。
4. **验证并配置。** 使用 `onboard --apply`；获取分支另需已审核工具、SHA256和两项确认参数。入口会串联获取、验证和新配置发布。部分覆盖、错误或取消即停止，不循环重试，不覆盖旧配置。
5. **验收并交给情报库。** 运行doctor，按用户目标少量抽检私聊、群聊、标签和时间范围。发生过获取时还要检查`recovery_review_required`：数据库可读不证明微信界面已恢复。用户事后确认官方微信正常登录、获取进程和授权窗口已结束后，才执行[恢复确认](access-troubleshooting.md#完成恢复检查)。不需要默认导出全部聊天；返回“是否可读、覆盖范围、还缺什么”。用户需要日报时才交给情报库，不默认生成HTML。

统一入口（由Codex运行，用户不用记）：

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/wechat-cli/scripts/access.sh" onboard
```

默认只检查。已有材料路径确定后，Codex加 `--source /absolute/path/to/access.json --database-root /absolute/path/to/db_storage --apply` 完成验证接入。无需重新获取的明文数据库也可以直接验证配置。

`provider_required`表示由Codex准备工具；`provider_review_required`表示来源或哈希尚未核验；`authorization_required`表示等待用户确认；`existing_configuration_requires_review`表示先排障，禁止覆盖。顶层 `ok:true`只表示命令完成；`state:ready`和`live_database_read_ok:true`表示配置可读，但仍要单独检查恢复状态。异常结果中没有key或原始聊天。

若失败，先用 `access.sh diagnose --support-summary` 读取白名单结果：`last_attempt_state` 区分启动前检查、provider无法启动和运行后失败，`last_attempt_diagnostics.phase` 标识停在何处。`shadow_policy_blocked` 是临时副本被系统策略拒绝的候选信号；`official_wechat_stop_failed` 是原微信未在限定时间内退出或仍有同名进程的候选信号。副本准备的 `official_wechat_scan_failed`、`official_wechat_process_unverified`、`other_wechat_install_running` 表示本机微信进程无法安全判定；PBKDF 兜底的 `pbkdf_process_*` 表示进程核验或正常退出失败，`pbkdf_shadow_*` 表示旧副本不可信。这些情况均应停止并核对恢复状态，不关闭系统保护、不强制杀进程、不循环重试。信号不等于拿到key，也不能单独判定所有微信版本不兼容。Reader 修订号 `2026-09-30.2` 起在启动provider前拦截其他用户及其他安装位置的同名微信进程；`.3` 增加本轮诊断，旧安装先保留个人材料并更新接入助手。

## 先检查，再配置

运行本Skill的 `reader.sh self-test`，再运行 `reader.sh access-plan --pretty`。

`access-plan` 只检查reader已有配置或显式选择的数据库与key文件，返回固定提示和数量，不输出账号路径、key和聊天内容。验证期间可创建并清理临时快照，不写持久配置，不运行外部provider。

指定账号时传 `--database-root /absolute/path/to/db_storage`；已有授权文件时可传 `--keys-file /absolute/path/to/access.json`。显式输入独立于当前正常配置检查。不得未经用户授权搜索其他工具的私有状态。

顶层 `ok: true` 只表示检查完成，不代表已连接微信。读取 `data.state` 和 `data.live_database_read_ok`：

| 状态 | 下一步 |
|---|---|
| ready | 复用现有配置，抽检所需数据；不要重新取key。 |
| ready_to_configure | 用相同输入运行setup，再doctor和抽检；目前未写配置。 |
| needs_access | 显式导入本人已有材料，或与用户确认外部获取路线；不要循环setup。 |
| partial / scan_incomplete | 说明缺失范围，缩小账号目录或显式提高max-files；不称完整历史。 |
| account_selection_required / database_layout_ambiguous | 请用户选择目标账号或准确目录，不自动合并。 |
| unsafe_key_permissions | 在本机收紧key文件权限，不输出其内容。 |
| dependency_required | 修复实际运行环境中的SQLCipher/Zstandard，不重新取key。 |
| verification_failed | 分别排查key、加密参数和结构兼容；不一定是key错。 |
| database_read_permission_denied | 实际提权worker读不到数据库；provider尚未启动。先核对完整磁盘访问授权的实际可执行进程，不重复取key、不扩大无关目录权限。 |
| database_files_not_found / database_header_unreadable | 检查本人账号目录和数据库同步状态；provider尚未启动。 |
| provider_failed | 本地worker-result.json只记录阶段、退出码、是否超时，不含provider原始日志。退出码不能证明失败原因；核对微信恢复，再决定下一步。 |
| previous_run_requires_review | 先完成官方微信恢复和配置核对，再人工审查恢复锁；不自动删锁重试。 |
| needs_database_location / database_missing | 核对登录、同步和目录位置。 |
| invalid_access_material / configuration_check_failed / filesystem_access_required / database_layout_unsupported | 本机针对性排障，不上传配置或盲目扩大权限。 |

## 缺key时的外部获取路线

先按实际系统和微信版本选择provider，审计并固定源码/发行版本和校验和，再说明副作用。历史成功不代表当前新机或全部版本均可用。不要从不明镜像执行脚本。

Mac可研究 [wxkey](https://github.com/r266-tech/wxkey)。上游bootstrap可能创建重签名微信副本、重启微信、访问进程/调试器，并把管理员密码保存到Keychain。这些是独立的需确认操作，不是普通只读查询。不要把工具自制密码弹窗当作安全保证，也不要让Agent从Keychain取出密码代输。新加入的 `access.sh` 可选助手走系统临时授权路线，但必须审核具体构建在root模式下确实跳过密码持久化；它尚未经过新机器实测，见[实验性接入说明](experimental-access.md)。

不将关闭SIP、修改主微信应用、持久保存管理员密码作为安装本Skill的隐含步骤。需要这些操作时，先针对具体影响征求用户确认。Windows必须独立核验provider，不使用Mac路径。没有已验证路线时明确说明，提供Demo或标注不完整的通知预览。

使用有界尝试：扫描有进展时不要因起初零命中反复重启；超时、取消或同样失败再次出现就停止，条件改变才重试。密码、key和数据库不得发给Agent、群友或Issue。

## 导入与验收

接入助手 `2026-10-05.1` 改进：同一raw key的大小写和SQLCipher字面量写法会先归一化；同时包含路径 `keys` 和 `salt_keys` 的材料会保留两类映射，不再丢失其中一类；规范化后冲突的同一salt或同一路径拒绝覆盖。`partial` 时也会先检查已有材料的相对路径转换，不直接要求再次获取。

`diagnose --source ... --support-summary` 分别报告现有配置和新材料的下一步：现有配置正常但新材料缺SQLCipher时，继续使用原配置，同时修复验证环境，不再将两者混成“key失败”。这些改进是导入与诊断兼容性修复，不代表已解决所有首次获取、macOS副本启动策略或Windows获取问题。

使用 `import-access --source ... --keys-file ... --verify` 显式导入授权材料。路径映射另需 `--database-root`；支持的schema-2文件带有 `db_root`。POSIX下key文件0600、父目录建议0700。不要覆盖现有可用材料，不能用 `--no-verify` 掩盖问题。

随后setup、doctor，并抽检已知私聊、群聊、关键词、用户需要的标签和时间范围。key数量、退出码0、JSON能解析都不代表全量成功。数据库key不等于图片key；本机未同步的聊天无法通过获取key补齐。

升级后先检查，缺材料确实是原因时才补采；日报不要常驻扫描内存。云端模型分析选中聊天时，区分本机读取和进入模型上下文，不宣称整个AI分析离线。

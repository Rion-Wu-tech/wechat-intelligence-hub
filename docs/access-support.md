# 接入卡住后的处理流程

## 先看六个不同的完成状态

| 状态 | 验收依据 | 不代表什么 |
|---|---|---|
| Skill 已安装 | Codex 能找到两个 Skill 与 Reader 命令 | 不代表能读取微信 |
| 材料已提供 | 本机存在本人提供的授权材料 | 不代表材料匹配当前账号或数据库 |
| 数据库实读成功 | `doctor` 的 `live_database_read_ok` 为 true，并能抽检已知消息 | 不代表本机保留全部手机历史 |
| 媒体路径可定位 | Reader 返回本机媒体路径，且文件确实存在 | 不代表图片文字、语音或视频内容已识别 |
| 媒体内容已理解 | 对选中的附件单独完成识别与人工核对 | 不代表所有历史附件均已处理 |
| 增量日报已生成 | 报告标明本次时间边界、读取覆盖和新增内容 | 不代表中间离线时未同步到电脑的消息已补齐 |

出现错误时先运行 `access.sh diagnose --support-summary`，看 `state`、`live_database_read_ok`、`recovery_review_required`、`last_attempt_state`、`last_attempt_diagnostics.phase` 和 `next_step_code`，不要只看命令退出码。`next_step_code` 是固定的当前操作类别，不是取钥成功证明。`use_existing_reader` 表示当前实读成功；`review_wechat_recovery` 优先于其他动作，须先确认官方微信恢复。历史 `last_attempt_state=worker_preflight_failed` 或 `provider_start_failed` 分别表示上次停在启动前检查、工具进程启动；`other_user_wechat_running` 和 `other_wechat_install_running` 表示启动前发现了不应由本次获取流程退出的微信进程。`signals` 中的 `shadow_policy_blocked` 表示上次检测到 macOS 临时副本启动策略拦截，`official_wechat_stop_failed` 表示原微信未在限定时间内退出或仍有同名进程。历史记录可能属于旧账号或旧版本，不能覆盖当前状态，也不应据此关闭系统保护或反复重试。

## 先确认在哪里运行

需要能够访问本人电脑文件并执行本地命令的Agent或终端。仅有网页聊天或远端云环境，不能直接打开电脑里的微信数据库。安装Skill只增加操作说明，不会替网页模型建立本机连接；不要为此将微信目录或key上传到云端。

## 已用其他工具跑通

先区分“应用界面能打开”和“真实聊天已可读取”。后者优先复用已有成果，Codex只在本人机器检查：

1. 已有授权材料：核对导出工具、文件格式及本人账号目录；使用`diagnose --source`临时验证，不把key贴到对话中。
2. 已有明文数据库：在本人明确指定的目录运行`access-plan --database-root ...`。保留原表结构且识别通过时可继续`setup`，无需额外key；结构不兼容时记录表名/缺失列，不重新获取。
3. 只有HTML、CSV或JSON导出：不是可以直接传给数据库Reader的文件。先核对字段与时间范围，再走Hub的导入接口；不能伪装成实时全量读取。

不自动扫描其他工具的私有配置，不运行聊天附件中的脚本，不把已解密数据库提交到Issue。

2026-10-26起，`onboard`会检查固定清单中的已知工具输出（只看文件是否存在，不读内容）：wxkey 的 `~/.config/wxcli/config.json`、wechat-cli 的 `~/.wechat-cli/all_keys.json`、wcdb-key-tool 的 `~/.wcdb-key-tool/wechat-passphrase.json`。命中时返回 `material_candidates`，脱敏摘要只给工具名 `known_material_tools`。口令格式需按各库 salt 派生，暂不能直接导入；不要因此重新获取。

## Mac失败或换最新版仍失败

已扫描完成但得到 0 个 key 时，社区资料（未经本仓库真机验证）指向两类原因：较新的 4.1.x 中 key 可能不再以可被字符串扫描的形式出现在内存；或派生只在登录、打开聊天等时刻发生。这类情况先记录微信完整版本和 `last_attempt_diagnostics.phase`，不要在同一版本上反复重试同一路线。

不承诺所有最新版兼容，不默认降级、关闭SIP或重复重启微信。先运行`diagnose --support-summary`，结合具体系统、微信完整构建、状态码及上一次失败阶段定位。多开/修改版与官方版应分别记录；不能只看版本号就断言原因。

存在恢复锁时，先核对官方微信是否已恢复、获取进程是否结束。使用者事后明确确认后，才按`finish-recovery`指引处理。恢复锁不是key失效的证明，也不能自动删掉来重试。

## Windows导入失败

使用原生Python和实际Windows路径，核对解释器架构、SQLCipher驱动、材料编码与ACL。连接和临时验证目录使用Windows ACL，而不是只看chmod模式位。已有目录权限不符合要求时明确失败，不自动扩大访问权限。

`diagnose --support-summary`是专用脱敏输出，仅包含白名单字段，不包含路径、姓名、聊天、密钥、自由文本错误或原始日志。它不会联网、启动获取或覆盖配置。诊断成功返回码不等于接入成功，要看`data.state`。

维护者提供Windows CI虚构数据库测试，覆盖真实ACL、中文路径及UTF-16材料的SQLCipher读取；CI通过仍不证明真实微信进程获取兼容。没有已验证材料时，首次获取依旧需要独立审计和知情授权。

Windows 上“Skill 安装成功”和“完整数据库接入成功”必须分开报告。社区某台机器的经验不能替代本仓库对当前微信版本的真机验收；目前不提供一键获取承诺。对已安装但缺材料的用户，先查本机已有合法材料或明文数据库，再给出具体缺口，不反复重装 Skill。

## 给 Issue 提供可复现信息

先由本人检查 `diagnose --support-summary` 的输出，再只提交：操作系统与版本、微信版本、Reader/助手版本、六阶段中卡住的阶段、固定状态/错误码、是否发生过微信退出及当前是否恢复、可在虚构数据上复现的步骤。只有标题“无法绑定微信”不足以判断是安装、授权、数据库还是日报问题。不要提交聊天截图、用户目录、数据库、密钥、原始日志或未经本人检查的诊断文件。提供反馈不等于授权维护者远程接管账号。

## 给本地Codex的提示词

> 更新微信CLI与微信个人情报库的公开代码和Skill，保留我的Profile、历史数据和访问材料。先用diagnose --support-summary定位接入问题，不自动获取新key。其他工具已有可用材料或已解密库时，只在本机验证复用；没有本地执行能力时明确告知。最终给出doctor、已知消息抽检结果和覆盖缺口，不把安装成功当成接入成功，不打印或上传密钥。

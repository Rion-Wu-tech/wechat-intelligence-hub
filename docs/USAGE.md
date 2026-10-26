# WeChat Intelligence Hub 使用指南

这份指南面向已经克隆仓库、希望在 Codex 中直接使用微信个人情报库的用户。日常使用优先说自然语言；命令行主要用于安装、诊断和手动渲染。

## 1. 安装与检查

```bash
git clone https://github.com/Rion-Wu-tech/wechat-intelligence-hub.git
cd wechat-intelligence-hub
./scripts/install.sh --with-sqlcipher
```

重新打开 Codex 后，可以让它执行完整的首次接入工作流：

```text
$wechat-cli 帮我接入这台电脑上我自己的微信。已有配置或key就复用；没有就帮我准备适配工具，说明影响并确认后获取，再完成验证和配置。不要让我复制key或手工拼命令。
```

三种常见状态：

- **完整数据库模式**：可以在本地授权和实际兼容范围内查询历史聊天与近期新增消息。
- **通知预览模式**：只能读取 macOS 实际保留的入站通知预览，不代表完整聊天记录。
- **缺少访问材料**：Codex按[首次接入流程](../skills/wechat-cli/references/access-onboarding.md)准备工具并请求必要确认；不兼容或暂不授权时可运行全虚构Demo，不能假装已读取真实聊天。

日常 Reader 不获取密钥、不重签名、不注入、不 Hook 微信。缺少访问材料且用户主动要求接入时，另见[实验性接入助手](../skills/wechat-cli/references/experimental-access.md)；它可能调用需单独审核和确认的外部获取工具，不能当作普通只读查询。

### 让Codex处理配置与排障

**还没安装：**直接把下面这段交给Codex。

```text
请读取 https://github.com/Rion-Wu-tech/wechat-intelligence-hub 的最新说明，帮我安装两个Skill并接入我自己的微信。检查本机环境、现有安装和配置，已有可用材料就复用。
由你执行安装、依赖处理、onboard检查、验证和配置；确实缺key时，你负责准备和核验工具，说明影响并经我确认后获取。我只负责登录微信和系统授权，不复制密码或key给你。
完成后告诉我是否可读、覆盖哪些范围，再帮我初始化微信个人情报库。
```

**已经安装，想更新：**安装器默认拒绝覆盖现有Skill，这是保护机制，不是需要删除数据重装。

```text
请把我已安装的微信CLI和微信个人情报库更新到该GitHub仓库的最新代码。
先确认实际使用的仓库、Skill路径和CLI运行时，对比现有改动；保留我的key、数据库配置、个人Profile、历史报告和个性化设置，不整目录删除重装。
同步适用的代码和工作流后运行self-test、onboard和doctor，验证仍能读取。如果onboard提示未知命令，检查是否仍调用旧运行时或旧Skill，不要重新取key。
```

**安装失败、读不到聊天或微信刚升级：**让Codex诊断，不需要自己猜是不是key错了。

```text
用 $wechat-cli 帮我排查本机微信读取问题。先查当前微信版本、CLI运行时、self-test和access-plan/onboard结果，区分缺依赖、目录或权限问题、多账号、缺key以及版本兼容问题。
已有可用配置不要重新获取key；失败配置不要直接覆盖。能修复的由你执行，涉及退出/重启微信、重签名副本或管理员权限时先说明影响并等我确认。
失败后不要无限重试，也不要要求我把密码、key、数据库或完整聊天发到这里。告诉我卡在哪一步、已经验证什么、下一步需要我做什么。
```

**Codex应当如何处理结果：**

| 检查结果 | Codex下一步 |
|---|---|
| `ready` | 复用配置，按需要抽检，不重新取key。 |
| `ready_to_configure` | 用已验证的同一组输入执行 `onboard --apply`，再验收。 |
| `dependency_required` | 找到实际CLI运行环境，修复缺少的依赖，而不是在无关Python环境反复安装。 |
| `needs_database_location` / `account_selection_required` | 核对本机登录和目录；多账号请用户选择，不能混读。 |
| `needs_access` / `provider_required` | 按首次接入工作流准备、审核固定版本工具；不让用户手工找key。 |
| `provider_review_required` / `authorization_required` | 完成来源核验或等待用户确认；检查成功不等于用户已授权。 |
| `other_user_wechat_running` / `other_wechat_install_running` | 启动前发现其他用户或其他安装位置的同名微信；停止本次获取，先核对进程范围，不由助手全局退出。 |
| `worker_preflight_failed` / `provider_start_failed` | 分别定位启动前检查和获取工具进程启动；看脱敏阶段码，不把两者都归因于 key 错误。 |
| `partial` / `scan_incomplete` | 说明缺失范围，按原因修复或调整扫描范围，不宣称完整历史。 |
| `existing_configuration_requires_review` / `verification_failed` | 保留原配置，核对材料、目录和版本兼容性，不自动覆盖或盲目重新获取。 |

目录权限问题应在获取前解决。取消授权或失败留下恢复锁时，先确认上次进程和微信状态；不能为了重试直接删锁、清空配置或删除旧key。管理员密码只在系统授权窗口输入。

如果需要反馈Issue，只提供脱敏后的系统/微信/CLI版本、固定错误代码和所处步骤。不要上传配置JSON、数据库、聊天导出、内存转储或未经检查的日志。详情见[首次接入流程](../skills/wechat-cli/references/access-onboarding.md)和[实验性获取的边界](../skills/wechat-cli/references/experimental-access.md)。

## 2. 建立个人 Profile

让系统了解你的身份、行业、当前项目和重点关系，日报排序会更贴合你正在做的事情：

```text
$wechat-intelligence-hub 帮我初始化微信个人情报库。先查找我现有的个人说明和当前计划；如果没有就给我准备清单，再只读检查现有微信标签并建议如何分类。
```

也可以使用命令行：

```bash
cd projects/wechat-intelligence-hub
python3 wechat_intelligence_hub.py profile-init \
  --owner-alias "你的微信昵称" \
  --personal-doc "/path/to/个人说明.md" \
  --plan-doc "/path/to/本月计划.md" \
  --priority-label "你的重点联系人标签"
```

个人 Profile、真实联系人、聊天数据库和生成报告只保存在本机，不应提交到 GitHub。

## 3. 最常用的自然语言请求

### 生成完整日报

```text
$wechat-intelligence-hub 生成过去 24 小时的完整微信情报日报，分析群聊、重点联系人、待回复事项、待兑现承诺和商业机会，同时输出 Markdown 和旗舰交互式 HTML。
```

“24 小时”和“48 小时”只是常用示例，不是时间上限。可以指定一天、一周、一个月、从某个日期至今，或明确的起止日期；实际可查询范围取决于本地已授权数据和索引覆盖情况。

已接入后想定期更新，不需要重装或重新获取材料。可以说：

```text
$wechat-intelligence-hub 查找上一次成功日报实际覆盖到的时间，从那个边界到现在刷新微信索引，给我新增的群聊与重点联系人变化；报告里注明本轮起止时间、未读到的会话和与上一轮重复的去重结果。只在已可读的数据库上运行，不重新接入微信。
```

没有可核实的上次覆盖时间，就明确询问或使用用户给出的日期，不能猜测“上次跑完”的时间。连续运行时也应说明“没有新消息”和“读取失败”的区别。电脑休眠或断网期间未同步到本机的消息不可能仅靠增量参数补齐；恢复联网后先确认微信已同步，再运行所需范围的报告。

图片、视频、文件有三层不同能力：消息记录存在、媒体文件路径可定位、内容可识别。前两层不等于 OCR 或看懂附件；高价值消息的附件需要定向核对，无法读取时标记“附件待核实”，不能据此确认商单、付款或交付。

例如：

```text
$wechat-intelligence-hub 生成 2026 年 8 月 1 日到 8 月 31 日的微信月度情报报告，同时输出 Markdown 和 HTML。

$wechat-intelligence-hub 总结今年 7 月至今微信中与「企业 AI 培训」相关的人、讨论、机会和后续进展。
```

### 查看今天应该先做什么

```text
$wechat-intelligence-hub 查看今天最需要我处理的 10 件事，说明原因和原始消息依据。
```

### 查看联系人进展

```text
$wechat-intelligence-hub 总结我和「联系人名字」最近聊到哪里，谁在等谁，还有哪些承诺没有完成。
```

如果涉及刚刚收到的消息，可以明确要求先刷新：

```text
$wechat-intelligence-hub 先刷新「联系人名字」的最新聊天，再告诉我是否需要回复。
```

### 生成回复草稿

```text
$wechat-intelligence-hub 根据最新上下文，给「联系人名字」生成一条符合我平时语气的简短回复草稿。
```

系统只生成本地草稿，不会发送微信。金额、日期、报价和承诺必须由使用者核对。

### 搜索所有微信聊天

```text
$wechat-intelligence-hub 搜索过去 7 天所有微信聊天里关于「AI 培训」的讨论，按联系人和群聊归纳并附上来源。
```

### 查看商业机会

```text
$wechat-intelligence-hub 找出过去 48 小时微信里的品牌商单、培训、咨询、项目合作和资源对接机会，区分已确认、高概率和待核实。
```

### 查看群聊情报

```text
$wechat-intelligence-hub 总结过去 24 小时所有活跃群聊：先按真实话题跨群归纳，再列值得阅读的重点群和商单信号。
```

### 查找适合复联的人

```text
$wechat-intelligence-hub 查看哪些品牌方、客户或合作伙伴已经到跟进时间，给出复联原因和一条简短建议。
```

## 4. 按信息、人、物、标签或事件定向查找

微信个人情报库不只生成日报。它也可以围绕一个明确对象定位原始消息、补齐上下文并生成针对性总结。

### 根据某条信息查找

```text
$wechat-intelligence-hub 查找微信里提到「预算 3 万、9 月上线」的相关消息，确认是谁提出的、对应什么项目以及后来有没有更新。
```

### 根据某个人查找

```text
$wechat-intelligence-hub 汇总我和「联系人名字」过去三个月的聊天，重点提取合作项目、报价、双方承诺和未完成事项。
```

### 根据某个产品或事物查找

```text
$wechat-intelligence-hub 搜索所有微信聊天里关于「产品名称」的消息，合并别名和相关关键词，按时间总结讨论变化。
```

这里的“物”可以是产品、工具、课程、公司、品牌、文件、链接或其他能够用名称和关键词描述的对象。名称可能有歧义时，系统应先列出候选范围，不应把不同对象混在一起。

### 根据微信标签查找

```text
$wechat-intelligence-hub 总结微信标签「品牌方」中最近一个月的联系人进展，列出待回复、等待对方、适合复联和已有合作机会。
```

标签用于确定联系人范围；也可以继续叠加时间、关键词和任务条件。系统只读查看标签，不会自动修改微信标签。

### 根据某个项目或事件查找

```text
$wechat-intelligence-hub 查找「活动名称」从首次出现到现在的全部相关讨论，按时间线总结参与者、关键决定、争议和下一步。
```

定向查找通常直接在 Codex 中回答；如果结果跨多个会话、需要长期留档或需要交互浏览，可以要求同时输出 Markdown 和 HTML。

## 5. Markdown 与 HTML 两种正式输出

交付形式默认使用 `auto`，按问题规模选择：

| 请求类型 | 默认交付 |
|---|---|
| 单个联系人、单个项目、关键词核实、回复建议 | 直接在 Codex 中回答 |
| 跨多个会话且需要保存 | Markdown |
| 完整多会话日报、周报、月报或指定时间段报告 | Markdown + HTML |
| 跨多会话的联系人、标签、项目或事件调查 | 按规模输出 Markdown，明确要求时同时输出 HTML |
| 需要搜索、筛选、点击展开的大范围报告 | Markdown + HTML |

完整日报的主要文件：

```text
wechat_daily_full.md
wechat-report/index.md
wechat-report/groups.md
wechat-report/group-topics.md
wechat-report/key-groups.md
wechat-report/contacts.md
wechat-report/radar.md
wechat_daily_report.html
```

其中：

- `wechat_daily_full.md` 是 Markdown 阅读入口。
- `wechat-report/` 保存各功能分区，便于复制、归档和继续编辑。
- `wechat_daily_report.html` 是包含全部正式分区的单文件交互报告。

HTML 版提供：

- 综合行动、群聊日报、重点联系人、商单信号雷达四个顶层入口
- 全分区搜索和路由导航
- 话题日报、重点群聊和群聊筛选切换
- 点击群名展开详情
- 根据活跃度和用户关注主题筛选群聊
- 原始外部链接跳转
- 三套可切换风格（信号雷达、编辑简报、台账），每套都有跟随系统、亮色、暗色；选择只保存在本机浏览器
- 打印或保存为 PDF
- 下载当前分区 Markdown

每位用户看到的界面结构一致，但报告内容会根据本地数据、时间范围、个人 Profile、微信标签和当前计划变化。

自己另写的专题 Markdown（例如结算复盘）不会自动出现在 HTML 里。在报告目录新建 `report_manifest.json` 指定分区，再重新渲染：

```json
{"sources": [{"path": "services_review.md", "page": "contacts", "title": "定制服务跟进"}]}
```

`page` 可选 `overview`、`groups`、`contacts`、`radar`。没有写进清单的 Markdown，渲染命令会在结尾列出来。

## 6. 手动生成双版本

通常让 Codex 完成语义整理和渲染即可。如果已经有一次完整运行的报告目录，也可以手动执行：

```bash
cd projects/wechat-intelligence-hub
python3 wechat_intelligence_hub.py render-bundle \
  /path/to/run-directory
```

指定输出位置或标题：

```bash
python3 wechat_intelligence_hub.py render-bundle \
  /path/to/run-directory \
  --out /path/to/run-directory/wechat_daily_report.html \
  --markdown-out /path/to/run-directory/wechat_daily_full.md \
  --title "我的微信情报日报"
```

`render-bundle` 应在群聊和重点联系人完成语义编辑后运行。`group_daily_digest.md` 和可选的 `group_daily_digest.html` 是机器初筛与审计产物，不能代替最终 Markdown 和旗舰 HTML。

如果系统没有安装 `pandoc`，各模块 Markdown 仍会保留，但 HTML 可能无法生成。按终端提示安装 `pandoc` 后重新运行 `render-bundle`。

## 7. 不读取真实数据的 Demo

先用仓库自带的全虚构样本确认分析和报告链路：

```bash
bash projects/wechat-intelligence-hub/scripts/run_demo.sh
```

指定 Demo 输出目录：

```bash
WECHAT_DEMO_OUT=/path/to/demo \
  bash projects/wechat-intelligence-hub/scripts/run_demo.sh
```

Demo 不会读取真实联系人、微信聊天或本地情报库。

## 8. 常见问题

### 为什么只收到文字回答，没有 HTML？

单个联系人、一个关键词或一条回复建议默认使用轻量交付。请明确要求“生成完整日报，同时输出 Markdown 和 HTML”。

### 为什么报告里没有某条消息？

先查看读取模式、索引截止时间和覆盖范围。“没有读取到”不等于“微信里没有”。微信升级后可要求 Skill 运行 `compat-check`。

### HTML 会把聊天上传到网页吗？

不会。它是写入本机的单文件报告，不需要把聊天上传到公开网站。不要把真实报告提交到 GitHub、Issue 或公开网盘。

### 浏览器里调整的群聊优先级会永久修改配置吗？

不会。HTML 中的选择保存在本地并可导出，只有使用者确认后才应写回个人 Profile。

### 能否自动发送回复？

不能。整个项目坚持微信只读，回复能力只生成草稿。

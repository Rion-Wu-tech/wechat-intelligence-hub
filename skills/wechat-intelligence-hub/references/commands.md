# Command Reference

只在需要构造具体命令时读取。所有命令通过统一包装器执行：

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh <command> <args>
```

## Status And Compatibility

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh home
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh db-status
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh compat-check --force
```

首次安装或计划变化时先检查/更新个人 Profile：

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh profile-status
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh profile-init \
  --owner-alias "本人微信昵称" \
  --personal-doc "/path/to/个人说明.md" \
  --plan-doc "/path/to/当前计划.md" \
  --priority-label "重点联系人"
```

已有 Profile 加 `--force` 更新；需要查看本机已有微信标签候选时加 `--inspect-wechat-labels`。详细规则见 `references/onboarding.md`。

## Recent Intelligence

刷新近期私聊和群聊，再分别生成群聊日报、重点联系人私聊日报和短总览：

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh db-index --scope sessions --session-type private,group --session-limit 80 --per-chat-limit 500 --default-24h --out output/db-index-brief
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh group-daily --hours 24 --out output/group-daily
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh contact-daily --hours 24 --out output/contact-daily
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh brief --hours 24
```

24/48 小时只是常用窗口。其他相对时长可调整 `--hours`；一周、一个月或自定义范围优先使用 `--since` 与 `--until`，并确保索引、群聊、联系人和总览使用相同边界。

群聊日报：

```bash
cd "${WECHAT_HUB_HOME:-$HOME/wechat-intelligence-hub}"
scripts/run_group_daily.sh
```

只看品牌方、中间人和自媒体博主私聊：

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh contact-daily --hours 24 --out output/contact-daily
```

交付边界：`group-daily` 先产生机器初筛，语义编辑后分别输出 `group_daily_topics.md` 和 `group_daily_groups.md`；`contact-daily` 负责关系推进，`brief` 只做跨报告行动总览。跨群 URL 在商单雷达中只出现一次，群聊主题不重复粘贴。

交付形式默认遵循 `delivery=auto`：定制问题直接在 Codex 回答；完整多会话日报、周报、月报或指定时间段报告在群聊和私信语义编辑后，生成短 Markdown 入口、完整分区 Markdown 和旗舰版交互 HTML：

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh render-bundle \
  output/run-24h-YYYYMMDD-HHMMSS
```

综合版生成短入口 `wechat_daily_full.md`、`wechat-report/` 下的综合行动/群聊/联系人/信号雷达 Markdown，以及四入口 `wechat_daily_report.html`。群聊页内切换话题日报、重点群聊和全部群聊筛选；筛选维度来自个人 Profile，自定义选择只保存在本地并可导出。网页提供全局搜索、分区路由、原链接跳转、打印/PDF、主题切换和当前分区 Markdown 下载。需要查看机器规则结果时，才给 `group-daily` 加 `--html`；该机器页不能替代最终综合日报。

今日动作与新线索：

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh today
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh inbox
```

## Person, Reply And Topic

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh person "品牌联系人"
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh person "品牌联系人" --refresh
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh reply "品牌联系人"
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh topic "培训" --keyword "赚钱" --days 7
```

定向调查可以组合对象与时间范围：

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh chat-history "联系人或群名" --since "2026-08-01" --until "2026-09-01" --query "项目名"
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh chat-search "产品名" --since "2026-08-01" --until "2026-09-01"
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh topic "项目名" --keyword "产品别名" --since "2026-08-01"
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh wechat-labels --label "品牌方" --out output/brand-contacts
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh db-index --scope labels --label "品牌方" --since "2026-08-01" --out output/brand-index
```

“某物”必须先转换为可搜索的名称、别名或关键词，例如产品、工具、课程、公司、品牌、文件或链接。匹配到多个同名对象时先消歧；不得将字符串命中直接冒充同一实体或同一事件。

`reply` 只生成本地草稿。先在个人 Profile 的 `owner_aliases` 配置使用者自己的微信昵称。若用户说“刚回复”，先刷新或读取最新聊天，再运行 `reply`。默认用最近 30 天本人跨联系人私聊的长度特征约束输出，并在当前联系人至少有 5 条本人消息后学习其专属口语；可用 `--style-days` 和 `--minimum-chat-messages` 覆盖 Profile。它先检查本人是否已经回复；已回复时返回“不用追发”，而不是继续套模板。

## Search Selection

搜索已索引数据库：

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh db-search 报价 --limit 30
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh db-search 商单 --chat "联系人或群名" --limit 20
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh db-search 品牌方 --since "2026-07-01" --out output/search-brand.md
```

实时搜索全微信并持久化命中：

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh chat-search "签证" --since "2026-01-01" --out output/search-visa
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh chat-search "报价" --chat "联系人或群名" --out output/search-quote-in-chat
```

索引多个搜索词后做本地聚合：

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh db-index --scope search --keyword 报价 --keyword 品牌方 --out output/db-index-search
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh db-search 报价 --limit 30
```

原则：`db-search` 快但只覆盖已索引数据；用户要求“所有微信聊天”或最新完整结果时使用 `chat-search`，不要先后重复跑三套相同搜索。

## Chat History And Shared Groups

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh chat-history "联系人名称" --limit 120 --out output/chat-history-zhao
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh chat-history "项目群名称" --since "2026-07-01" --query agent --out output/chat-history-okx-agent
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh common-groups "原负责人" "新负责人" --since "2026-04-01" --out output/common-groups-rimbo
```

共同群结果使用微信成员 ID 核验。确认交接后更新：

`contacts/交接关系.csv`

## Tagged Contacts

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh db-index --scope labels --per-chat-limit 500 --out output/db-index-labels
```

标签联系人来源：

`contacts/微信标签联系人.csv`

## Reactivation

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh reactivation --index-first --label 重点客户 --since "2025-11-01" --inactive-days 21 --out output/reactivation
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh reactivation --out output/reactivation
```

只把 `今天优先看` 中已经到期的对象当作即时复联。`待交接跟进`、`纯佣低优先级` 和 `我方主动放弃` 不得统称为合作失败。

## Repeated Links

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh db-links --since "2026-07-01" --min-chats 2 --out output/cross-links-history.md
```

输出时每个 URL 只展示一次，后面列出现群聊、发布者和可能角色。

## Opportunity Pipeline

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh opportunity-sync --since "2026-07-01" --dry-run
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh opportunity-sync --since "2026-07-01"
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh opportunities
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh opportunities --include-candidates
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh opportunities --due-only
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh opportunities --chat "品牌联系人"
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh opportunity-update 12 --status waiting --stage "待 brief" --follow-up 2026-08-20 --note "等待项目方反馈"
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh opportunity-maintain
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh opportunity-maintain --apply
```

把商机设为 `waiting` 必须同时给 `--follow-up YYYY-MM-DD`，日期格式不对会直接报错；用户没给日期时先问，不要替他编一个。

`opportunities` 默认只显示正式机会；加 `--include-candidates` 才同时查看未经人工确认的候选。`opportunity-maintain` 默认只预览超过 14 天没有新证据的候选，明确确认后才使用 `--apply`。

人工分流：

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh triage 12 pursue --follow-up 2026-08-20 --next-action "发送合作方案"
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh triage 12 wait --follow-up 2026-08-22 --note "等待品牌确认预算"
```

导入商业执行系统前先预览：

```bash
$HOME/.codex/skills/rion-commercial-os/scripts/commercial.sh import-wechat --wechat-id 12 --dry-run
$HOME/.codex/skills/rion-commercial-os/scripts/commercial.sh import-wechat --wechat-id 12
```

## Feedback And Exclusion

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh feedback-add --target-type chat --target "测试群" --verdict ignore --note "不是真实商单"
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh feedback-add --target-type chat --target "品牌方联系人" --verdict confirmed
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh feedback-add --target-type chat --target "低价值群" --verdict low_priority
```

永久排除群名稳定子串写入：

`contacts/排除名单.txt`

## Cleanup

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh cleanup --raw-days 7 --report-days 30
```

默认仅预览。只有用户明确确认后才能使用 `--apply`。

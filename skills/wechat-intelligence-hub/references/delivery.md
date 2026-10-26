# Delivery Modes

根据问题规模选择最轻的有效交付，不要因为使用了 Skill 就自动制造文件。以下名称是 Skill 的交付模式，可在自然语言请求中指定，不是 `wechat_intelligence_hub.py` 的全局命令行参数。

## Modes

- `auto`：默认。根据对象数量、时间跨度、是否需要复用和是否需要交互浏览自动选择。
- `text`：只在当前 Codex 回答中交付，不保存报告文件。
- `md`：当前回答给行动摘要，同时保存最终 Markdown。
- `html`：先生成各模块的最终 Markdown，再从本轮报告目录生成综合 HTML；不得交付机器初筛 HTML 冒充最终版。
- `all`：Markdown、HTML、证据附录和结构化文件都保留，主要用于审计、调试或大型复盘。

用户没有明确指定时使用 `auto`：

| 请求 | 默认交付 |
|---|---|
| 单个联系人最新回复、是否需要回、回复草稿 | `text` |
| 单一品牌、项目、关键词或不超过 5 个对象 | `text` |
| 6 至 15 个对象、跨多群归因、需要后续复用 | `md` |
| 正式多会话日报、周报、月报或指定时间段报告 | `html`（同时保留分区 Markdown） |
| 跨多会话的联系人、标签、项目或事件调查 | `md`；明确需要交互浏览时用 `html` |
| 大范围报告且用户需要搜索、筛选或点击展开 | `html` |
| 识别规则调试、漏报复核、开源回归 | `all` |

对象数量只是辅助判断。即使消息很多，只要用户问的是一个明确问题，也优先直接回答。即使对象较少，只要涉及长期决策、持续跟进或需要留档，也可以保存 Markdown。

## Delivery Contract

- `text` 仍需完成必要的实时读取、索引和证据核对；只是不把中间产物作为用户交付。
- 直接回答优先包含：结论、当前阶段、是否需要行动、一条建议或草稿、必要的群名/时间/发言人证据。
- 只有报告型请求才展示报告路径。不要向用户罗列内部 JSON、CSV 和机器初筛文件。
- `group_daily_digest.md`、`group_daily_digest.html`、JSON、CSV 和 appendix 是扫描或审计产物，不是默认最终答复。
- 群聊最终 Markdown 使用 `group_daily_topics.md` 和 `group_daily_groups.md`，私信使用 `contact_daily_brief.md`。`group_daily_brief.md` 只作旧版兼容。完整日报在各模块语义编辑完成后运行：

```bash
$HOME/.codex/skills/wechat-intelligence-hub/scripts/hub.sh render-bundle \
  /path/to/run-directory \
  --out /path/to/run-directory/wechat_daily_report.html
```

- `render-bundle` 生成短入口 `wechat_daily_full.md`、`wechat-report/` 下的分区 Markdown，以及 `wechat_daily_report.html`。HTML 只包含四个顶层入口：综合行动、群聊日报、重点联系人、商单信号雷达。群聊日报页内切换话题日报与重点群聊；支持全局搜索、分区路由、原链接跳转、打印和当前分区 Markdown 下载。证据附录和机器初筛留在本地供审计，不出现在阅读导航中。
- 未安装 `pandoc` 时保留各模块 Markdown，并明确说明 HTML 未生成；不要退回交付机器初筛 HTML。
- 结算复盘、定制服务跟进等新增专题 Markdown 不会自动进入 HTML。在报告目录放 `report_manifest.json`，写明它属于哪个分区后再渲染：`{"sources": [{"path": "services_review.md", "page": "contacts", "title": "定制服务跟进"}]}`。`page` 只能是 `overview`、`groups`、`contacts`、`radar`。`render-bundle` 会列出未进入 HTML 的 Markdown；看到这行提示时，要么补清单，要么告诉用户这份内容只在 Markdown 里。
- `brief` 开头的“覆盖”一行说明本次窗口读了多少条。出现“只纳入最新 N 条”时，这份简报不完整：缩短时间范围重跑，或改用分模块日报，不要把它当完整结论交付。

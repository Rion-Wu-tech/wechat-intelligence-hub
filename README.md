# WeChat Intelligence Hub

微信个人情报库：把本地微信聊天变成可检索、可核查、可行动的个人情报，包括联系人历史、群聊主题、待回复、承诺、商机、复联线索，以及任意指定时间范围的情报报告。同时内置全新 **WeChat Slim 微信存储智能瘦身引擎**，通过 APFS 硬链接秒省数十 GB 空间。

这不是 Prompt 大礼包，而是一个独立的微信旗舰项目。仓库同时提供只读数据入口、情报工作流与无损瘦身工具，并配有可执行入口、边界、测试和全虚构样例。

当前首发版本为 `v0.9.2-preview.2`。微信相关代码已经具备公开测试条件，但不是“安装后自动读取所有人的完整微信历史”：完整数据库模式需要本人授权的本地数据库和访问材料。Reader 核心不获取密钥、不重签名、不注入、不 Hook 微信；可选的实验性接入助手有独立授权和副作用边界，见下文。

## 一个产品，三个 Skill / 模块

| Skill / Project | 作用 | 状态 |
|---|---|---|
| `wechat-cli` | Rion 自有的只读 Reader 入口；v0.9.2-preview.2 已覆盖旧版接口、schema-2 salt-key 授权导入、WCDB 压缩消息与本机实读验收 | 依赖层 / Preview |
| `wechat-intelligence-hub` | 把微信记录转成日报、待回复、承诺、商机和复联线索 | 用户入口 / Flagship |
| `wechat-slim` | 专治 Mac 微信存储占用：APFS 原生硬链接秒级去重、核心人脉防删白名单与无损归档 | 维护工具 / New |

微信能力在代码中分成多层，方便独立测试和维护；对用户仍是一套产品、一次安装：

- `projects/rion-wechat-reader/`：Rion 自有的 clean-room 只读 Reader 核心。
- `skills/wechat-cli/`：Reader 的统一 Agent 入口，默认只调用 Rion 自有 Reader；只有使用者显式设置 `RION_WECHAT_CLI_BIN` 时才调用兼容后端。
- `skills/wechat-intelligence-hub/`：Agent 的调用入口与判断规则。
- `skills/wechat-slim/`：微信存储智能瘦身 Agent Skill。
- `projects/wechat-intelligence-hub/`：确定性本地情报引擎、微信瘦身引擎（`wechat_slim.py`）、虚构样例和测试。

Rion 的通用 Skill 合集 `rionwu-skills` 只负责收录、发现和链接本项目，不复制微信读取器源码或 Git 历史。

## 安装

### 直接让Codex安装和配置

把下面这段发给Codex即可，不需要自己逐条执行命令：

```text
请从 https://github.com/Rion-Wu-tech/wechat-intelligence-hub 安装微信CLI和微信个人情报库，接入这台电脑上我自己的微信。
先读取仓库说明，检查是否已经安装、当前配置是否可用；已有可用配置或key就复用，不覆盖我的个人Profile和数据。
缺少依赖由你处理；确实缺key时，由你核验并准备固定版本工具，说明影响、经我确认后执行。我负责登录微信和完成系统授权，不会把密码或key发给你。
完成数据库验证和配置后，告诉我能读取哪些范围、还有什么未就绪；再协助初始化个人情报库。遇到错误请定位并修复，不要把一堆命令交给我，也不要无限重试。
```

这是有人确认关键操作的接入工作流，不是无条件、无人值守的解密。已有安装如何升级、微信升级后如何排障，见[配置与排障提示词](docs/USAGE.md#让codex处理配置与排障)。

### 手动安装入口

**先确认接入条件：安装成功不等于已能读取聊天。** 已有本机访问材料可直接验证、导入；没有key时，由Codex按[五步首次接入工作流](skills/wechat-cli/references/access-onboarding.md)准备固定版本工具，经审核和明确确认后尝试获取，可能重启微信和重签名副本。用户只需登录、确认影响和完成系统授权，不需要复制key。仓库不捆绑获取工具，安装和日常日报均不会触发获取；macOS新机器获取路径尚未实测，不保证所有版本可用。请勿向维护者、社群或Issue发送key、密码和数据库。

克隆仓库：

```bash
git clone https://github.com/Rion-Wu-tech/wechat-intelligence-hub.git
cd wechat-intelligence-hub
```

安装完整产品：

```bash
./scripts/install.sh --with-sqlcipher
```

不传 Skill 名称时会安装 `wechat-cli`、`wechat-intelligence-hub` 及其本地引擎。即使只指定 `wechat-intelligence-hub`，安装器也会自动补齐它依赖的 `wechat-cli`；用户不需要手工拼装两套组件。

安装后可以直接对Codex说：

> 用 $wechat-cli 帮我接入这台电脑上我自己的微信。已有配置或key就复用；没有就帮我准备工具，说明影响并确认后获取，再完成验证和配置。

安装 `wechat-cli` Skill 时会同时安装 Rion 自有的 `rion-wechat-reader` 核心。新用户无需第三方二进制即可检查本机覆盖范围，并在 macOS 上读取系统实际保留的微信通知预览；该模式只覆盖入站预览，不能代表完整聊天记录。v0.9.2-preview.2 的公开接口已与旧 `wechat-cli 1.6.19` 的 29 项只读工具、266 个输入字段对齐，可读取用户显式提供的 schema-2 salt-key 授权材料，并安装隔离 SQLCipher 与 Zstandard 运行依赖。当前本机已完成新旧 CLI 真实数据对照、当前 macOS 图片/视频/文件路径验证和微信个人情报库端到端索引验证；其他微信版本仍按能力矩阵逐项积累。

需要独立命令行入口时只安装一个 CLI：

```bash
projects/rion-wechat-reader/install.sh --with-sqlcipher
rion-wechat-cli self-test
rion-wechat-cli self-test --require-sqlcipher
rion-wechat-cli access-plan --pretty
```

`ready` 表示复用已有配置；`ready_to_configure` 才继续用相同输入运行 `setup`；`needs_access` 表示缺少访问材料，不要重复运行setup。JSON顶层 `ok: true` 仅表示诊断完成，请查看 `data.state`。部分覆盖、驱动缺失、多账号和权限问题会分别给出下一步。

完整历史和实时数据库读取要求有权访问的本地数据库与访问材料，且仅覆盖已同步到本机的数据。只读 Reader 与可选接入助手的边界见 [Reader 说明](projects/rion-wechat-reader/README.md)。没有数据库读取条件时，仍可运行全虚构 Demo，验证索引、判断与报告链路。

安装 `wechat-intelligence-hub` 时，脚本会同时把经过隐私扫描的本地引擎放进 Codex 目录，正常调用无需再配置 `WECHAT_HUB_HOME`。先用虚构数据运行 Demo：

```bash
bash projects/wechat-intelligence-hub/scripts/run_demo.sh
```

只有在开发或调试仓库源码时，才需要临时指定引擎路径：

```bash
export WECHAT_HUB_HOME="$PWD/projects/wechat-intelligence-hub"
```

安装 `wechat-cli` 和 `wechat-intelligence-hub` 后，建议先做一次个性化初始化。它会把你正在做的事、个人背景和关系标签变成日报的排序依据：

```bash
cd projects/wechat-intelligence-hub
python3 wechat_intelligence_hub.py profile-init   --owner-alias "你的微信昵称"   --personal-doc "/path/to/个人说明.md"   --plan-doc "/path/to/本月计划.md"   --priority-label "你的重点联系人标签"
```

个人说明可以包含身份、业务、擅长领域、资源、约束和长期目标；当前计划可以包含近期目标、正在推进的项目、交付/收入优先级和截止时间。如果还没有这些文档，直接运行 `profile-init` 即可生成本地准备清单；在补齐前系统仍能生成通用报告，但会标明尚未个性化。

微信标签不需要照搬维护者的名字。推荐按自己的工作流建立 2–5 类，例如客户、同行、渠道、供应商、自媒体网友或品牌方。可用 `profile-init --inspect-wechat-labels` 只读查看已有标签并生成候选建议；精确关键词检索始终可以覆盖全部微信记录，不受标签限制。

也可以在 Codex 中直接说：

```text
$wechat-intelligence-hub 帮我初始化微信个人情报库。先查找我现有的个人说明和当前计划；如果没有，给我准备清单，再只读检查现有微信标签并建议如何分类。
```

重新打开 Codex 后，可以直接说：

```text
$wechat-intelligence-hub 查看过去 24 小时微信里最需要我处理的事情
```

## 在 Codex 中怎么使用

安装并完成初始化后，不需要记命令行参数，可以直接用自然语言调用：

```text
$wechat-intelligence-hub 生成过去 24 小时的完整微信情报日报，分析群聊、重点联系人、待回复事项和商业机会。

$wechat-intelligence-hub 总结我和「联系人名字」最近聊到哪里，还有什么承诺没有完成。

$wechat-intelligence-hub 搜索过去 7 天所有微信聊天里关于「AI 培训」的讨论。

$wechat-intelligence-hub 查找 8 月 1 日至今所有与「某个产品」有关的信息，合并同一件事的上下文并总结进展。

$wechat-intelligence-hub 总结微信标签「品牌方」里最近一个月值得跟进的人和事情。

$wechat-intelligence-hub 查看今天最需要我处理的 10 件事。

$wechat-intelligence-hub 根据最新聊天上下文，帮我给「联系人名字」写一条符合我语气的回复草稿。
```

### 完整日报同时输出 Markdown 和 HTML

24/48 小时只是常用日报窗口，不是时间上限。用户可以指定一天、一周、一个月、某段起止日期或已有索引覆盖的更长范围。完整的多会话复合报告默认保留两种正式版本：

- **Markdown 版**：适合阅读、复制、归档和继续交给 AI 加工。
- **交互式 HTML 版**：适合搜索、筛选和浏览，包含综合行动、群聊日报、重点联系人和商单信号雷达四个入口。

直接在 Codex 中说：

```text
$wechat-intelligence-hub 生成过去 24 小时的完整微信情报日报，同时输出 Markdown 和旗舰交互式 HTML。
```

主要文件包括：

```text
wechat_daily_full.md          # Markdown 总入口
wechat-report/                # 分区 Markdown
wechat_daily_report.html      # 旗舰交互式报告
```

HTML 版支持全局搜索、分区导航、话题日报/重点群聊/群聊筛选切换、群聊展开、原链接跳转、明暗主题、打印和当前分区 Markdown 下载。报告内容会根据每位使用者的本地聊天、个人 Profile 和当前计划生成，界面与公开仓库中的旗舰渲染器保持一致。

除了按时间生成综合报告，还可以围绕某条信息、某个人、某个群、某个产品/物品、某个微信标签、某个项目或某件具体事件定向查找：系统会先定位相关消息和上下文，再按会话、时间和事件关系去重总结。单个对象和回复建议默认直接在 Codex 中回答，不会为了一个简单问题额外生成网页。如果希望定向调查也保存成双版本，请在请求中明确说“同时输出 Markdown 和 HTML”。

更完整的首次使用、常用提问、输出模式和命令行说明见 [`docs/USAGE.md`](docs/USAGE.md)。

---

## 🍏 微信存储瘦身与安全去重 (WeChat Slim)

针对 Mac 微信占用几十甚至上百 GB 存储的顽疾，仓库内置了专用的 **WeChat Slim** 安全瘦身套件。

### 核心特性

1. **APFS 原生硬链接秒级去重（零损省空间）**：
   - 将多群转发的同一文件（视频、PPT、图片等）在底层磁盘扇区合并为指向同一物理数据块的硬链接；
   - 磁盘立省数十 GB 物理空间，微信聊天窗口内文件原样可开、点击秒读。
2. **核心人脉防删白名单（一票否决权）**：
   - 支持将重要联系人、客户或核心项目群加入保护名单；
   - 支持文件名关键词（如“合同、报价、发票”）保护；
   - 执行清理时，命中白名单的文件统统跳过，绝无误删。
3. **安全废纸篓与外置硬盘归档**：
   - 拒绝 `rm -rf` 粗暴删除，默认移入 macOS 系统废纸篓，随时可放回原处；
   - 支持一键将大文件完整迁移归档至外置移动硬盘或 NAS。
4. **数据库 100% 物理隔离**：
   - 底层代码对 `db_storage` 及所有 `*.db`, `*.sqlite`, `*.wcdb` 强制跳过，绝不篡改聊天记录数据库。

### 快速上手

```bash
# 启动交互式终端向导
python3 wechat_slim.py

# 扫描当前微信存储占用
python3 wechat_slim.py scan

# 执行多群文件 APFS 硬链接去重
python3 wechat_slim.py dedup --action hardlink -f

# 清理 90 天前大于 10MB 的视频与文件（移动至废纸篓）
python3 wechat_slim.py clean --days 90 --min-size 10MB --types video,file -f

# 管理防删白名单
python3 wechat_slim.py tag --list
python3 wechat_slim.py tag --add "核心客户" --keywords "合同,发票"

# 启动本地可视化仪表盘
python3 wechat_slim.py web --port 8080
```

在 Codex 中亦可直接使用 `$wechat-slim` 进行自然语言清理与空间巡检，详见 [`skills/wechat-slim/SKILL.md`](skills/wechat-slim/SKILL.md)。

---

## 隐私与安全

- 微信相关能力只读，不发送消息，不操作微信 UI。
- 真实聊天、联系人、Profile、数据库和输出报告不得提交到 Git。
- 仓库中的聊天、账号、品牌和金额样例均应为虚构数据。
- Reader 的数据库兼容性可能随微信版本变化；通知预览只是入站、非完整的降级来源。
- 运行任何涉及账号、支付、发布或外部写入的动作前，由使用者最终确认。
- 独立实现、商标和第三方关系说明见 [`NOTICE.md`](NOTICE.md)。

发布前运行：

```bash
./scripts/validate.sh
```

<a id="社群"></a>

## 付费社群｜Rion AI 实践与商业化 Club

**这是自愿加入的长期付费社群，不是免费的项目用户群。使用本仓库不需要付费入群。**

微信个人情报库只是其中一个项目。群里还会交流 AI 工具与行业应用、自媒体、工作流和商业化，不限于自媒体创作者。

- **知识库**：整理 AI、自媒体与商业化的方法、教程和资料，目前逐步完善。
- **Skill 与工具**：优先在群内同步项目进展和更新，分享安装与使用教程。
- **实践交流**：不定期分享真实项目复盘、工作流和行业应用经验。
- **资源与合作**：分享相关活动、行业资源，有合适的合作时与群友交流。

**微信号：`a668899universe`，添加时请备注「付费社群」。** 具体权益与参与边界见[社群介绍](docs/COMMUNITY.md)；当前价格以 Rion 最新公开招生说明及付款前确认的报价为准，了解后再决定是否加入。

只想使用工具？请先查看[安装说明](#安装)、[使用教程](docs/USAGE.md)与[配置和排障提示词](docs/USAGE.md#让codex处理配置与排障)。可复现的问题可通过 [Issue](https://github.com/Rion-Wu-tech/wechat-intelligence-hub/issues) 反馈，请勿提交聊天记录、数据库或密钥。社群费不是软件购买费，也不会解除技术接入限制或代付第三方服务费用。

<a id="services"></a>

## AI 指导、企业培训、定制与商务合作｜单独收费

除付费社群外，也接受以下服务需求咨询，先确认目标与可行性，再约定范围和报价：

- **一对一 AI 实践指导**：围绕个人的 AI 工具使用、Skill 配置、内容创作或工作流问题，按小时预约；开始前确认本次目标与准备事项。
- **企业／团队 AI 培训（B 端）**：面向企业、团队和具体岗位，结合实际业务需求安排 AI 工具使用与实操训练。可沟通的方向包括办公提效、内容与营销、知识库、智能体和自动化工作流；具体课程按需求评估设计，不代表所有专题均有现成课程。根据参与人数、培训内容、时长及线上／线下形式单独报价。
- **Skill、智能体与自动化工作流定制**：面向个人或企业的具体任务，评估可行性后按项目报价，提前约定交付物、验收标准、排期与维护范围。
- **品牌合作／产品推广**：接受 AI 工具、产品及相关品牌的内容合作咨询，结合实际体验与受众匹配度评估，单独确认内容形式、发布渠道、排期及报价；不承诺曝光量、涨粉或转化结果。

**微信号：`a668899universe`，添加时请按需求备注「AI 指导」「企业培训」「定制」或「商务合作」。** 请简述所在行业、希望解决的问题与预期时间；企业培训可补充参训岗位、人数及线上／线下偏好，品牌合作可补充产品链接、合作目标、预算范围和排期。咨询时请勿发送企业机密、客户敏感信息或账号凭据。

以上服务与社群分别收费，**不要求先加入社群，也不默认包含在社群费用内**；已有明确约定的成员权益不受影响。实施代做、课后陪跑、维护、软件商业授权、第三方服务及线下差旅等是否包含，均在报价前单独确认。不承诺收益或无限支持，公开项目仍可按各自许可证使用，无需购买这些服务。

## 支持这个项目 / Support the Project

如果微信个人情报库帮你少翻了聊天记录、找到了值得跟进的机会，欢迎给项目点个 Star。你的支持会让它持续更新，也让更多人用好自己的聊天信息。

If WeChat Intelligence Hub saves you time reviewing chats or helps you spot an opportunity worth following up, please consider giving the project a Star. Your support helps it keep improving.

[前往 GitHub，点个 Star / Star on GitHub](https://github.com/Rion-Wu-tech/wechat-intelligence-hub)

## License

本项目采用 **GNU Affero General Public License v3.0 only（AGPL-3.0-only）**。你可以学习、运行、修改和用于商业活动；分发修改版本，或通过网络向用户提供修改版本时，必须履行 AGPL 对应源码等义务。

需要闭源集成、专有发行、OEM、白标或不希望承担 AGPL 义务的企业，可申请[单独商业授权](COMMERCIAL-LICENSE.md)。加入 Rion 的付费社群不会自动改变软件许可证；社群提供的是安装适配、工作流配置、案例、持续更新和实践支持。

第三方依赖继续遵循各自许可证，仓库根许可证不会替换依赖项目的授权声明。已经依据 AGPL 获得的公开版本许可不可撤销，但未来版本可以采用不同的发布方式。

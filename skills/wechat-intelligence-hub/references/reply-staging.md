# 确认后填入微信草稿（实验性）

此功能独立于只读 Reader，默认不运行。不发消息、不按 Enter、不调用发送按钮、不模拟真人躲避检测。平台可能仍将 UI 写入视为自动化；手动发送不等于官方授权或零风险。

## 当前支持范围

- macOS、用户已经手动打开的当前会话、纯文本、空输入框。
- 两条显式分开的路线：`native` 使用 AXValue 写入并读回；`computer_use` 由 Codex 通过原生电脑工具看图核对后粘贴，再看图确认。都不调用第三方微信发送库。
- 不自动搜索联系人、不自动切换会话、不支持 Windows UI、不支持图片/文件草稿。
- 账号和 chat ID 由只读查询取得，由用户对照当前界面确认。AX 快照不是稳定身份认证，不能仅凭昵称就宣称机器核实了联系人。
- 原生路线遇到微信不在前台、焦点不正确、AX 树不完整时停止。只接受 `/Applications/WeChat.app`，不能因 bundle ID 相同而操作影子副本。
- AX不可用时可以另起显式的电脑工具路线，但不能盲点坐标：先获取最新截图，核对官方应用、会话标题、可见上下文、输入框无文字和附件；再按该截图定位输入框。不自动从已开始或结果未知的写入切换路线。
- HTML 仍是静态报告，未增加 localhost 桥接或放宽 CSP；通过 Codex 调用入口。

## 执行步骤

1. 最新只读消息生成建议，先展示准确收件人和完整正文。
2. 用户打开该会话、聚焦空消息框。只运行 `reply-draft probe --chat-label '当前显示的完整名称'` 检查能力，不激活微信或申请权限。
3. `prepare` 绑定用户指定账号、聊天ID、正文摘要及当前窗口快照。此时不修改微信。
4. 用户确认本条正文、当前账号/会话和无附件后，才能 `approve`。不得把“开发该功能”“上一条确认”“模型认为可发送”当成本条审批。
5. `stage` 消耗一次性审批，再核验界面未变；只写入草稿。用户复核并亲自发送。

```bash
hub.sh reply-draft probe --chat-label '联系人显示名'
hub.sh reply-draft prepare --account '本人账号ID' --chat '聊天ID' \
  --chat-label '联系人显示名' --text-file /private/path/reply.txt
# 用户看过prepare返回的正文并明确确认后：
hub.sh reply-draft approve DRAFT_ID --sha TEXT_SHA256 \
  --confirm-current-account-and-chat --confirm-no-attachments
hub.sh reply-draft stage DRAFT_ID --approval ONE_TIME_APPROVAL
hub.sh reply-draft show DRAFT_ID
# 仅取消尚未开始填入的草稿，不修改微信：
hub.sh reply-draft cancel DRAFT_ID
```

草稿准备后10分钟失效，审批后2分钟失效。会话内容变化也会阻止填入，重新核对后生成新草稿。`staged`仅表示读回匹配，不代表发送；`staging/unknown`表示中断或结果不确定，必须人工查看，禁止自动重试。已有文字不会清空，附件为空需用户确认，不声称AX能检测所有附件状态。

## AX不可用时的电脑工具路线

这是 Agent 协作流程，不是CLI自行识图，也不是无人值守自动回复。输入框是否为空、身份及正文是否一致依赖操作者/模型按最新截图核对；不能把人工声明当成程序验证的事实。截图只在当前受授权的电脑工具上下文核对，不发送给JEV或公开Issue。

1. 用电脑工具按完整路径选择官方微信。用户明确指定目标时可以通过可见UI导航到该会话；否则由用户先打开。只在正确、稳定的窗口上继续。看到用户操作窗口的提示立即重新观察，不抢焦点或重复操作。
2. 截图核对目标及空输入框后，`prepare` 增加 `--visual-observation cua:本轮观察编号`。此编号只是审计引用，不是截图哈希或机器身份认证。
3. 展示完整正文和目标；用户逐条确认后，`approve` 另加 `--confirm-fresh-visual-review`。未获确认不得填写审批参数。
4. 再次观察会话和空输入框；`visual-handoff` 只释放一次待填入正文，不执行UI动作。不能用它把原生草稿变成另一条路线。
5. 用电脑工具按最新画面聚焦输入框，观察确认，再 `paste(text, {format:'text'})` 一次。macOS电脑工具会恢复原剪贴板。不使用 Enter、发送按钮、全选删除或脚本固定坐标。
6. 获取填入后的截图，检查完整正文仍在输入框且未出现为新消息。两分钟内记录 `visual-result`；不能证明时记录 `unknown` 并让用户检查，绝不再次粘贴。

```bash
hub.sh reply-draft prepare --account '本人账号ID' --chat '聊天ID' \
  --chat-label '显示名' --text-file /private/path/reply.txt \
  --visual-observation cua:observation-1
# 用户确认后：
hub.sh reply-draft approve DRAFT_ID --sha TEXT_SHA256 \
  --confirm-current-account-and-chat --confirm-no-attachments --confirm-fresh-visual-review
hub.sh reply-draft visual-handoff DRAFT_ID --approval ONE_TIME_APPROVAL \
  --observed-label '显示名' --confirm-empty-composer
# 上一步之后由Codex电脑工具单次粘贴并观察，CLI自身没有粘贴或发送动作。
hub.sh reply-draft visual-result DRAFT_ID --receipt RECEIPT --outcome staged \
  --observed-label '显示名' --observed-sha VERIFIED_TEXT_SHA256
```

成功状态为 `staged_visual`，验证方式为 `operator_visual_review`，不能称为AX读回验证。`visual-result`不执行或撤销UI操作；缺回执、回执错配、正文或目标不匹配、超时不会报告成功。`show`不暴露一次性审批或回执。取消只适用于尚未开始填入的草稿，不能用取消伪装未知结果已经解决。

本地草稿正文保存在 `~/.local/state/wechat-reply-drafts/drafts.sqlite`，目录0700、文件0600，不进入开源仓库。`prepare`输出含正文，仅用于用户审批，不可作为公开支持日志。尚未完成不同微信版本的实机写入矩阵，不能称已全面可用。

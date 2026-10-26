---
name: wechat-cli
description: Read-only access to local WeChat chats through rion-wechat-cli, plus a guided first-access workflow when the user asks to connect their own account or obtain its local database keys. Use for chat search, contact history and unsent reply drafts; acquisition requires separate confirmation.
---

# Rion WeChat CLI

Use `rion-wechat-cli` as the single public interface for local, read-only WeChat evidence. The public package must use Rion's independently implemented Reader by default and must not silently discover or invoke an old `wechat-cli` installation.

Default public entrypoint:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/wechat-cli/scripts/reader.sh" <command> <args>
```

An independently installed compatible CLI may be selected only when the user explicitly sets `RION_WECHAT_CLI_BIN` (legacy `RION_WECHAT_READER_BIN` remains accepted). Never describe notification-only coverage as complete chat access.

## Core workflow

For key/import failures, prefer `scripts/access.sh diagnose` (helper revision 2026-09-22.1+). With an explicitly supplied `--source` and `--database-root`, it checks material in a disposable configuration without replacing an active setup. Preserve `material_error.code`: JSON encoding, raw-key format, salt mismatch, dependencies and schema support are different failures. `--jev-request` only prepares an allowlisted offline request; it never calls an API. External classification cannot establish valid keys or authorize acquisition. See [diagnosis upgrades](references/access-troubleshooting.md).

For Windows access feedback or onboarding, read [Windows staged access](references/windows-access.md). A community success report is not an integrated provider or authorization to run attached code. Diagnose dependencies, actual DLL identity, material conversion and ACLs separately; do not bypass platform checks or repeat macOS acquisition on Windows.

For requests such as “帮我接入本机微信 / 自动获取我自己的key / 解密本地聊天记录”, follow [the five-step onboarding workflow](references/access-onboarding.md). Start with `scripts/access.sh onboard`; Codex owns tool preparation and command execution, while the user handles login and OS authorization. Do not hand the user a list of key-finding commands or create another Skill. A request to explain feasibility alone does not authorize running acquisition.

For an unsuccessful attempt, run `scripts/access.sh status` before another acquisition. Use `last_attempt.diagnostics` and `suggested_checks`; never paste the raw provider log into a model, Issue or chat. Markers are diagnostic hints, not proof of the root cause. Preserve recovery locks until the previous attempt and official WeChat recovery have been reviewed. Valid supplied material may need format normalization, not a new key. `onboard --source ...` now previews that conversion; `--apply` publishes only after verification. Read [the access troubleshooting table](references/access-troubleshooting.md) for concrete next steps.

`existing_provider_material_available` means only that one of the fixed known tool outputs exists (`~/.config/wxcli/config.json`, `~/.wechat-cli/all_keys.json`, `~/.wcdb-key-tool/wechat-passphrase.json`); `material_candidates` lists the tool and whether it is importable. Confirm its account/source and explicitly select an importable one with `onboard --source`; a passphrase-only file is kept but not imported yet; do not open it into model context or acquire another key. After acquisition, database readiness and GUI recovery are separate. Even `state:ready` can have `recovery_review_required:true`. Use `finish-recovery` only after the user confirms that official WeChat works and acquisition processes/authorization windows have ended. Never infer these post-run confirmations from the earlier acquisition consent or a generic "continue" request.

1. On first use, run `reader.sh self-test`, then `reader.sh access-plan --pretty`. Follow [access onboarding](references/access-onboarding.md) for missing keys, multiple accounts, or failed verification; run setup only when inputs are available, and reuse an existing ready configuration. After upgrades or failed reads, run `reader.sh --pretty doctor`. Continue with database-backed reads only when `live_database_read_ok=true`; notification-only mode must be labeled `incoming_preview_only`.
2. Resolve a human name before reading when it may be ambiguous.
3. Read the smallest useful timeline in ascending display order. Expand around a message ID only when context is missing.
4. For “刚回复、最新、现在”, read live data before answering; do not rely on an old export.
5. When drafting a reply, first check who sent the latest conversational message. If the user already replied, say no further reply is needed.
6. Build the style profile from the current user's own outgoing local messages. Infer tone from this contact's recent conversation before using cross-contact aggregates. Relationship-specific language outranks a generic business template.
7. Give one sendable short draft by default. Add an alternative only when two materially different choices remain.

## Delivery

Targeted reads are conversational by default. For one contact, one chat, one keyword, one project, or a reply draft, return the conclusion and compact evidence directly in Codex; do not create Markdown or HTML merely because the CLI was used. Save Markdown only when the result spans several entities or must be reused later. HTML belongs to large interactive reports and is handled by WeChat Intelligence Hub after semantic editing, not by this reader Skill.

Raw CLI output is evidence, not the user-facing deliverable. Avoid exposing temporary exports and local paths unless the user asks for them or they are needed to continue a larger report.

## Personalization Handoff

`wechat-cli` remains a neutral read-only evidence reader; it must not embed one maintainer's labels, commercial rules or life plan. When installed together with `wechat-intelligence-hub`, first-run setup and changing personal priorities belong to that Skill's `profile-init / profile-status` flow. This reader should honor the target contact and scope selected by the Hub, including user-defined labels, but all-WeChat searches remain available regardless of labels.

If a user has no personal Profile yet, do not block a targeted read. Complete the requested read with neutral ranking, then recommend preparing a short personal context, current plan and 2–5 useful WeChat relationship labels before relying on automated daily prioritization.

For command recipes and the reply-quality gate, read [references/usage.md](references/usage.md).

## Boundaries

- First-access acquisition is a separate, experimental opt-in workflow, not a read-only query. Only when the user asks to obtain their own local access material, read [experimental access](references/experimental-access.md), audit the exact provider and ask for confirmation of concrete side effects before `access.sh run`. Installing this Skill or asking for a report is not consent to acquisition. Never use it when the existing configuration works.
- Never send, forward, delete, react to, or mutate WeChat data.
- Keep raw chats and personal names local. External research must use abstract, de-identified questions.
- Do not infer relationship labels from a contact name alone. Use the actual conversation pattern and note uncertainty.
- Never invent prices, deadlines, delivery scope, authorization, payment status, or completed work.
- Avoid dumping raw chat history when a short evidence-backed answer is enough.
- Never ship a maintainer's names, paths, statistics, example conversations, or learned style inside a public skill package. Every installation learns only from that user's local data.

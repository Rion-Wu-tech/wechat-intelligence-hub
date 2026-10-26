#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
from html import escape as html_escape
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any, Iterable

from opportunity_store import (
    ALL_STATUSES,
    FEEDBACK_VERDICTS,
    TRIAGE_DECISIONS,
    add_feedback,
    expire_stale_candidates,
    init_opportunity_schema,
    list_feedback,
    list_opportunity_inbox,
    list_opportunities,
    list_today_opportunities,
    sync_opportunity_candidates,
    triage_opportunity,
    update_opportunity,
)
from maintenance import apply_cleanup, find_cleanup_candidates
from intelligence_views import (
    PROMISE_COMPLETION_TERMS,
    PROMISE_TERMS,
    brief_report,
    home_report,
    infer_promise_action,
    open_promise_rows,
    person_report,
    reply_report,
    topic_report,
)
from report_bundle_html import render_report_bundle
from report_security import protect_document, require_sanitizer, safe_href, sanitize_fragment
from message_time import local_timestamp as display_timestamp, parse_timestamp, register_time_sql, timestamp_epoch, validate_window
from message_identity import conversation_key, source_chat_id


def default_wechat_reader_path() -> str:
    configured = os.environ.get("WECHAT_READER_BIN")
    if configured:
        return str(Path(configured).expanduser())
    codex_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    candidates = [
        codex_root / "skills" / "wechat-cli" / "scripts" / "reader.sh",
        Path(__file__).resolve().parents[2] / "skills" / "wechat-cli" / "scripts" / "reader.sh",
        codex_root / "bin" / "rion-wechat-cli",
        Path.home() / ".local" / "bin" / "rion-wechat-cli",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(candidates[0])


DEFAULT_WECHAT_READER = default_wechat_reader_path()


DEFAULT_PROFILE: dict[str, Any] = {
    "profile_version": 2,
    "owner_aliases": ["我", "me", "自己"],
    "owner_social_handles": [],
    "labels": {
        "priority": [],
        "commercial": [],
        "creator": [],
        "reactivation": [],
    },
    "project_chat_terms": ["未结", "初稿", "终稿", "对接群", "交付群", "合作群", "项目群"],
    "reply_style": {
        "history_days": 30,
        "minimum_chat_messages": 5,
    },
    "contact_daily": {
        "scope": "hybrid",
    },
    "group_recap_senders": {},
    "group_recap_time_windows": {},
    "context": {
        "setup_status": "needs_context",
        "personal_documents": [],
        "current_plan_documents": [],
    },
    "intelligence_priorities": {
        "focus_areas": [
            "AI", "赚钱", "培训", "商单", "出海", "产品", "Web3",
            "自媒体运营与增长", "合作", "B端AI赋能",
        ],
        "priority_keywords": [],
        "deprioritize_keywords": [],
        "custom_topics": {},
    },
}
ACTIVE_PROFILE: dict[str, Any] = json.loads(json.dumps(DEFAULT_PROFILE, ensure_ascii=False))
ACTIVE_PROFILE_PATH: Path | None = None


def merge_profile(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(base, ensure_ascii=False))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_profile(merged[key], value)
        else:
            merged[key] = value
    return merged


def profile_candidates(explicit_path: str | None = None) -> list[Path]:
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())
    env_path = os.environ.get("WECHAT_HUB_PROFILE")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.extend(
        [
            Path(__file__).resolve().parent / "config" / "profile.local.json",
            Path.home() / ".config" / "wechat-intelligence-hub" / "profile.json",
        ]
    )
    return candidates


def load_profile(explicit_path: str | None = None) -> tuple[dict[str, Any], Path | None]:
    if explicit_path:
        explicit = Path(explicit_path).expanduser()
        if not explicit.is_file():
            raise SystemExit(f"找不到 Profile：{explicit}")
        candidates = [explicit]
    else:
        candidates = profile_candidates()
    selected = next((path for path in candidates if path.is_file()), None)
    if selected is None:
        return merge_profile(DEFAULT_PROFILE, {}), None
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取 Profile：{selected}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Profile 顶层必须是 JSON 对象：{selected}")
    profile = merge_profile(DEFAULT_PROFILE, payload)
    required_lists = [
        ("owner_aliases", profile.get("owner_aliases")),
        ("owner_social_handles", profile.get("owner_social_handles")),
        ("project_chat_terms", profile.get("project_chat_terms")),
        ("labels.priority", profile.get("labels", {}).get("priority") if isinstance(profile.get("labels"), dict) else None),
        ("labels.commercial", profile.get("labels", {}).get("commercial") if isinstance(profile.get("labels"), dict) else None),
        ("labels.creator", profile.get("labels", {}).get("creator") if isinstance(profile.get("labels"), dict) else None),
        ("labels.reactivation", profile.get("labels", {}).get("reactivation") if isinstance(profile.get("labels"), dict) else None),
    ]
    invalid = [name for name, value in required_lists if not isinstance(value, list)]
    if invalid:
        raise SystemExit(f"Profile 字段必须是数组：{selected} / {', '.join(invalid)}")
    recap_senders = profile.get("group_recap_senders")
    if not isinstance(recap_senders, dict) or any(not isinstance(value, list) for value in recap_senders.values()):
        raise SystemExit(f"Profile 字段 group_recap_senders 必须是群名到发布者数组的映射：{selected}")
    recap_windows = profile.get("group_recap_time_windows")
    if not isinstance(recap_windows, dict) or any(not isinstance(value, list) for value in recap_windows.values()):
        raise SystemExit(f"Profile 字段 group_recap_time_windows 必须是群名到时间窗口数组的映射：{selected}")
    reply_style = profile.get("reply_style")
    if not isinstance(reply_style, dict):
        raise SystemExit(f"Profile 字段 reply_style 必须是对象：{selected}")
    for key in ("history_days", "minimum_chat_messages"):
        value = reply_style.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SystemExit(f"Profile 字段 reply_style.{key} 必须是正整数：{selected}")
    contact_daily = profile.get("contact_daily")
    if not isinstance(contact_daily, dict):
        raise SystemExit(f"Profile 字段 contact_daily 必须是对象：{selected}")
    contact_scope = contact_daily.get("scope")
    if contact_scope not in {"hybrid", "priority_labels_only"}:
        raise SystemExit(
            f"Profile 字段 contact_daily.scope 只能是 hybrid 或 priority_labels_only：{selected}"
        )
    context = profile.get("context")
    if not isinstance(context, dict):
        raise SystemExit(f"Profile 字段 context 必须是对象：{selected}")
    for key in ("personal_documents", "current_plan_documents"):
        if not isinstance(context.get(key), list):
            raise SystemExit(f"Profile 字段 context.{key} 必须是数组：{selected}")
    priorities = profile.get("intelligence_priorities")
    if not isinstance(priorities, dict):
        raise SystemExit(f"Profile 字段 intelligence_priorities 必须是对象：{selected}")
    for key in ("focus_areas", "priority_keywords", "deprioritize_keywords"):
        if not isinstance(priorities.get(key), list):
            raise SystemExit(f"Profile 字段 intelligence_priorities.{key} 必须是数组：{selected}")
    custom_topics = priorities.get("custom_topics")
    if not isinstance(custom_topics, dict) or any(not isinstance(value, list) for value in custom_topics.values()):
        raise SystemExit(
            f"Profile 字段 intelligence_priorities.custom_topics 必须是主题名到关键词数组的映射：{selected}"
        )
    return profile, selected


def configure_profile(explicit_path: str | None = None) -> None:
    global ACTIVE_PROFILE, ACTIVE_PROFILE_PATH
    ACTIVE_PROFILE, ACTIVE_PROFILE_PATH = load_profile(explicit_path)


def profile_list(key: str, nested_key: str | None = None) -> list[str]:
    value: Any = ACTIVE_PROFILE.get(key)
    if nested_key is not None:
        value = value.get(nested_key) if isinstance(value, dict) else None
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def configured_labels(kind: str) -> list[str]:
    return profile_list("labels", kind)


def configured_self_names(extra_names: list[str] | None = None) -> list[str]:
    names = profile_list("owner_aliases")
    names.extend(name.strip() for name in (extra_names or []) if name.strip())
    return list(dict.fromkeys(names))


def configured_reply_style_int(key: str, fallback: int) -> int:
    mapping = ACTIVE_PROFILE.get("reply_style")
    value = mapping.get(key) if isinstance(mapping, dict) else fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(1, parsed)


def configured_group_recap_senders(chat: str) -> list[str]:
    mapping = ACTIVE_PROFILE.get("group_recap_senders")
    if not isinstance(mapping, dict):
        return []
    normalized_chat = chat.strip().casefold()
    senders: list[str] = []
    for group_marker, values in mapping.items():
        marker = str(group_marker).strip().casefold()
        if not marker or marker not in normalized_chat or not isinstance(values, list):
            continue
        senders.extend(str(value).strip() for value in values if str(value).strip())
    return list(dict.fromkeys(senders))


def configured_group_recap_time_windows(chat: str) -> list[str]:
    mapping = ACTIVE_PROFILE.get("group_recap_time_windows")
    if not isinstance(mapping, dict):
        return []
    normalized_chat = chat.strip().casefold()
    windows: list[str] = []
    for group_marker, values in mapping.items():
        marker = str(group_marker).strip().casefold()
        if not marker or marker not in normalized_chat or not isinstance(values, list):
            continue
        windows.extend(str(value).strip() for value in values if str(value).strip())
    return list(dict.fromkeys(windows))


def message_time_in_windows(message_time: str, windows: list[str]) -> bool:
    if not windows:
        return True
    match = re.search(r"(?:T|\s)(\d{2}):(\d{2})", message_time)
    if not match:
        return False
    minute = int(match.group(1)) * 60 + int(match.group(2))
    for window in windows:
        window_match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", window)
        if not window_match:
            continue
        start = int(window_match.group(1)) * 60 + int(window_match.group(2))
        end = int(window_match.group(3)) * 60 + int(window_match.group(4))
        if start <= end and start <= minute <= end:
            return True
        if start > end and (minute >= start or minute <= end):
            return True
    return False


def is_owner_sender(sender: str) -> bool:
    normalized = sender.strip().casefold()
    for alias in configured_self_names():
        candidate = alias.casefold()
        if normalized == candidate:
            return True
        if len(candidate) >= 4 and re.match(rf"^{re.escape(candidate)}[\s(（_\-]", normalized):
            return True
    return False


STAGE_ORDER = [
    "新线索",
    "待回复",
    "待报价",
    "待 brief",
    "待确认报价/排期",
    "待创作",
    "待品牌审核",
    "待发布",
    "已发布待结算",
    "已发布待数据跟进",
    "已收款",
]

SIGNAL_RULES = [
    ("资源机会", "新线索", 5, r"品牌方|项目方|资源|内推|推荐|求推荐|有人想|谁想|想接|可接|招募|KOL|博主|达人|加热|红包|转发|转推|扩散|帮忙顶|推文链接"),
    ("合作意向", "新线索", 2, r"合作|商单|推广|投放|广告|赞助|sponsor|collab|partnership|campaign"),
    ("报价", "待报价", 4, r"报价|价格|费用|预算|rate|quote|price|cost|RMB|人民币|USD|AUD|\$|￥"),
    ("brief", "待 brief", 4, r"brief|需求|要求|素材|卖点|脚本|大纲|资料|产品介绍"),
    ("排期", "待确认报价/排期", 4, r"排期|发布时间|什么时候发|哪天发|周[一二三四五六日天]|今天|明天|后天|deadline|timeline|schedule"),
    ("待回复", "待回复", 5, r"在吗|看到回复|麻烦确认|确认一下|方便吗|请问|什么时候方便|ping|follow up|keep me posted"),
    ("创作", "待创作", 3, r"初稿|草稿|写完|内容|文案|thread|单帖|文章|quote repost|QR"),
    ("审核修改", "待品牌审核", 4, r"审核|过稿|修改|反馈|review|approve|revision|change"),
    ("待发布", "待发布", 4, r"发布链接|已发布|已经发布|已经发出|上线后|\bpost(?:ed)?\b|\bpublish(?:ed)?\b|go live|发出去了"),
    ("结算", "已发布待结算", 5, r"结算|结款|月结|单结|付款|先付|先款|打款|收款|汇款|款项|稿费|全款|发票|专票|普票|开票|尾款|回款|对账(?!号)|定金|订金|预付|账期|invoice|payment|paid|settle"),
    (
        "发布后数据",
        "已发布待数据跟进",
        5,
        r"浏览量.{0,16}(?:低|少|只有)|增加.{0,16}曝光|补(?:量|曝光|互动)|"
        r"找.{0,16}(?:KOL|博主|达人).{0,16}(?:转发|加热)|"
        r"(?:KOL|博主|达人).{0,16}(?:转发|加热)|多\s*quote|"
        r"数据统计.{0,16}(?:发布后|天|截止)|数据回传|回传数据",
    ),
]

DEFAULT_VAULT_KEYWORDS = [
    "合作",
    "商单",
    "推广",
    "投放",
    "报价",
    "预算",
    "brief",
    "排期",
    "发布时间",
    "初稿",
    "审核",
    "修改",
    "发布",
    "结算",
    "付款",
    "invoice",
    "sponsor",
    "campaign",
    "quote",
    "payment",
    "品牌方",
    "项目方",
    "资源",
    "推荐",
    "谁想",
    "想接",
    "KOL",
    "博主",
    "加热",
    "红包",
    "转发",
    "转推",
]

# Asking how invoicing or payment works before a deal exists is a terms question.
# It must not be read as "published, awaiting settlement".
PRE_DEAL_SETTLEMENT_TERMS = re.compile(
    r"(?:合作|下单|签约|开始|确定|接单)(?:之)?前"
    r"|(?:如果|要是|假如|若是?)[^。！？!?\n]{0,6}合作|合作的话"
    r"|(?:可以|能|能否|可否|是否|能不能|可不可以|方便|支持)[^。！？!?\n]{0,10}(?:开|出|提供|走)[^。！？!?\n]{0,6}(?:发票|专票|普票|invoice|对公)"
    r"|(?:发票|专票|普票|invoice)[^。！？!?\n]{0,12}(?:可以|能|能否|方便)[^。！？!?\n]{0,6}[吗么?？]"
    r"|(?:怎么|如何|怎样|什么方式|哪种方式)[^。！？!?\n]{0,6}(?:结算|付款|打款|收款|开票)"
    r"|(?:能不能|可不可以|能否|可否|是否)[^。！？!?\n]{0,8}(?:结算|付款|打款|收款|开票)"
    r"|(?:结算|付款|收款|打款)(?:方式|流程|周期|节点|规则|条款)|账期|预付|定金|订金|先款后|先付"
    # Asking whether a payment arrangement is acceptable, or which of two
    # arrangements applies, is still negotiation.
    r"|(?:接受|介意|同意)[^。！？!?\n]{0,3}[吗么?？]|(?:可以|行)[吗么][?？]?\s*$"
    r"|(?:付|款|结算)[^。！？!?\n]{0,14}(?:还是|或者)|(?:还是|或者)[^。！？!?\n]{0,14}(?:付|款|结算)",
    re.I,
)
# Only things that already happened, or a chase for money owed, count as
# progress; "发布后付尾款" is a plan.
SETTLEMENT_PROGRESS_TERMS = re.compile(
    r"已(?:经)?(?:发布|发出|上线|交付|开票|付款|打款|结算|支付|到账|收到)|发票已|已开(?:好|具)|开好了|"
    r"安排(?:付款|打款|结算)|(?:付|打)款(?:了|完)|结算(?:流程)?(?:了|完)|(?:打|转|付|汇)(?:过去|过来)了|"
    r"到账|收到款|回款|对账(?!号)|催|麻烦[^。！？!?\n]{0,10}(?:安排|处理|跟进)|还没(?:到|收到|付|打|结)|"
    # Asking when an invoice will actually be issued is a chase, not a capability question.
    r"开出来|开票[^。！？!?\n]{0,4}进度",
    re.I,
)


def settlement_signal(content: str) -> tuple[str, str, int]:
    """Classify a message that hit the settlement keywords as (category, stage, priority)."""
    if PRE_DEAL_SETTLEMENT_TERMS.search(content) and not SETTLEMENT_PROGRESS_TERMS.search(content):
        return "结算条款", "待确认报价/排期", 4
    return "结算", "已发布待结算", 5


ACTIVE_STAGES = {"待回复", "待报价", "待 brief", "待确认报价/排期", "待创作", "待品牌审核", "待发布", "已发布待数据跟进"}
SETTLEMENT_STAGES = {"已发布待结算", "已收款"}
RESOURCE_TERMS = re.compile(r"品牌方|项目方|资源|内推|推荐|求推荐|有人想|谁想|想接|可接|招募|KOL|博主|达人|加热|红包|接龙|转发|转推", re.I)
DIRECT_DEAL_TERMS = re.compile(r"brief|报价|预算|排期|审核|修改|发布|结算|付款|invoice|payment|campaign|sponsor|商单|合作|推广|投放", re.I)
LOW_SIGNAL_CHAT_TERMS = re.compile(r"加热|红包|BoostClub|运营群|交流群|社群|朋友们|Community|Club|矩阵", re.I)
GROUP_TOPIC_RULES = [
    ("商单/合作", r"商单|合作|推广|投放|品牌方|项目方|campaign|sponsor|KOL|达人|博主|brief|报价|预算|排期|审核|发布"),
    ("红包加热/互动", r"加热|红包|接龙|点赞|评论|转发|转推|收藏|quote|三连|四连|Boost|boost"),
    ("结算/收款", r"结算|付款|打款|收款|invoice|PayPal|支付宝|微信转账|银行卡|云账户|payment|paid"),
    ("培训/活动/项目", r"培训|工作坊|workshop|分享会|峰会|大会|沙龙|直播|训练营|讲师|授课|项目合作|咨询项目"),
    ("AI 产品/模型", r"AI|Agent|Claude|OpenAI|Gemini|模型|大模型|智能体|workflow|工作流|提示词|prompt|MCP|Codex"),
    ("内容增长/自媒体", r"涨粉|流量|曝光|推文|帖子|文章|thread|内容|选题|账号|蓝V|X平台|Twitter|小红书|公众号|YouTube"),
    ("Web3/交易", r"Web3|钱包|交易所|OKX|Binance|币|合约|空投|token|USDT|BTC|ETH|链上|DeFi"),
    ("工具/产品推荐", r"工具|插件|网站|链接|开源|GitHub|API|SDK|教程|文档|模板|自动化"),
    ("招聘/外包/咨询", r"招聘|招募|内推|兼职|实习|外包|接单|客户|咨询|培训|B端|讲师"),
]
MONETIZATION_TERMS = re.compile(
    r"商单|合作|推广|投放|报价|预算|结算|付款|收款|invoice|campaign|sponsor|品牌方|项目方|"
    r"招募|内推|兼职|外包|接单|客户|咨询|培训|讲师|返佣|分成|佣金|变现|"
    r"红包|加热|接龙|奖励|激励|收益|付费|付费社群",
    re.I,
)
DEAL_OPPORTUNITY_TERMS = re.compile(
    r"商单|品牌方|投放|推广|广告|赞助|campaign|sponsor|KOL|达人|博主|"
    r"谁想接|有人想接|想接|可接|接单|招募.{0,12}(?:KOL|达人|博主)|"
    r"(?:推荐|求推荐).{0,12}(?:KOL|达人|博主)|名额|brief|返佣|佣金|CPS|CPA|"
    r"红包|加热|接龙|三连|四连",
    re.I,
)
TRAINING_PROJECT_TERMS = re.compile(
    r"企业培训|线上培训|线下培训|AI\s*培训|培训项目|想搞.{0,8}培训|"
    r"工作坊|讲师|授课|课时|课程体系|成套.{0,6}(?:课程|线上课)|"
    r"咨询项目|顾问项目|FDE|外包项目|项目合作|合作项目|"
    r"(?:谁|有没有人|有人).{0,12}(?:接|做).{0,8}(?:项目|培训|咨询|工作坊)",
    re.I,
)
GROUP_DIGEST_ACTIONABLE_DEAL_TERMS = re.compile(
    r"(?:来(?:了|个)?|新来).{0,4}(?:商单|单子)|(?<!所)(?:有个|有一批|有新(?:的)?|有一轮)商单|"
    r"大单.{0,120}(?:campaign|品牌方|达人|KOL|原创|申请)|"
    r"(?:campaign|品牌方).{0,120}(?:开放申请|报名开启|招募)|"
    r"(?:投放|广告).{0,48}(?:预算|接广告|私信|找我)|"
    r"(?:商单|合作|投放|推广).{0,20}(?:招募|报名|名额|预算|报价|找人|找博主|需要博主|可接|想接)|"
    r"(?:谁|有没有人|有人|大家).{0,16}(?:想接|可接|接单|报名)|"
    r"(?:品牌方|项目方).{0,20}(?:招募|找|需要|预算|报价|名额|投放|合作)|"
    r"(?:招募|寻找?|需要|推荐).{0,16}(?:KOL|达人|博主)|"
    r"(?:预算|稿费|保底|佣金).{0,12}(?:元|人民币|RMB|USD|U|美金|澳元)|"
    r"(?:brief|需求).{0,16}(?:报名|招募|名额|预算|报价|发布时间)",
    re.I,
)
GROUP_DIGEST_ACTIONABLE_TRAINING_TERMS = re.compile(
    r"(?:有没有|谁|需要|招募?|寻找?|推荐|想搞|准备|计划|有个|有一场)"
    r".{0,24}(?:企业培训|AI\s*培训|培训项目|讲师|工作坊|咨询项目|项目合作|教练)|"
    r"(?:找|需要).{0,12}(?:讲师|培训师|老师|教练|顾问)|"
    r"(?:企业培训|AI\s*培训|培训项目|讲师|工作坊|咨询项目|项目合作|教练)"
    r".{0,24}(?:招募?|需要|寻找?|合作|报名|预算|报价|课时|授课|推荐|机会|需求)",
    re.I,
)
GROUP_DIGEST_ACTIONABLE_PROJECT_TERMS = re.compile(
    r"(?:项目合作|咨询项目|顾问项目|外包项目|FDE)"
    r".{0,24}(?:招募|找|需要|合作|报名|预算|报价|推荐|人选|档期)|"
    r"(?:招募|找|需要|报名|预算|报价|推荐|人选)"
    r".{0,24}(?:项目合作|咨询项目|顾问项目|外包项目|FDE)",
    re.I,
)
GROUP_DIGEST_ACTIONABLE_EVENT_TERMS = re.compile(
    r"(?:活动|分享会|工作坊|workshop|峰会|大会|沙龙|直播|训练营|黑客松|hackathon|VibeHacks?)"
    r".{0,24}(?:报名|招募|嘉宾|讲师|合作|赞助|举办|主办|时间|地点|名额|免费|付费)|"
    r"(?:报名|招募|嘉宾|讲师|合作|赞助|举办|主办)"
    r".{0,24}(?:活动|分享会|工作坊|workshop|峰会|大会|沙龙|直播|训练营|黑客松|hackathon|VibeHacks?)|"
    r"(?:做一场|办一场|举办|准备|计划).{0,48}"
    r"(?:活动|分享会|工作坊|workshop|峰会|大会|沙龙|直播|训练营|黑客松|hackathon|VibeHacks?)",
    re.I,
)
GROUP_DIGEST_ACTIONABLE_JOB_TERMS = re.compile(
    r"(?:招聘|急招|招募|内推|岗位|实习|兼职|外包|接单)"
    r".{0,24}(?:人|同学|博主|运营|设计|开发|顾问|讲师|简历|报名|需要)|"
    r"(?:找|需要).{0,16}(?:实习生|兼职|外包|运营人员|设计师|开发者|开发人员|工程师|顾问)",
    re.I,
)
GROUP_DIGEST_ACTIONABLE_MONEY_TERMS = re.compile(
    r"(?:有偿|奖金|奖励|佣金|分成|返佣|课时费|稿费)"
    r".{0,20}(?:报名|参与|可接|招募|任务|合作|名额|结算)|"
    r"(?:报名|参与|可接|招募|任务|合作)"
    r".{0,20}(?:有偿|奖金|奖励|佣金|分成|返佣|课时费|稿费|保底)|"
    r"(?:收益|收入|能赚|可赚|赚个|赚到).{0,16}(?:破千|上千|过千|\d+(?:\.\d+)?\s*(?:元|万))|"
    r"保底\s*\d+(?:\.\d+)?\s*(?:元|人民币|RMB|USD|U|美金|澳元|万)|"
    r"(?:短平快项目|拉新任务).{0,20}(?:收益|佣金|结算|窗口|报名|教程)",
    re.I,
)
DISCUSSION_DEADLINE_TERMS = re.compile(
    r"(?:今天|今晚|明天|本周|周[1-7一二三四五六日天]).{0,10}"
    r"(?:前|内|截止|截稿|发布|报名|交付|确认|安排|排期)|"
    r"截止|截稿|截至|最晚|排期|发布时间",
    re.I,
)
DISCUSSION_INFORMATION_TERMS = re.compile(
    r"正式发布|新品发布|正式上线|已经上线|现已上线|上线了|.{2,15}[，,:]\s*上线|"
    r"正式开源|宣布开源|开源了|重大更新|发布新版本|新版本发布|"
    r"入选|中标|政策变化|规则变化",
    re.I,
)
DISCUSSION_SUBSTANCE_TERMS = re.compile(
    r"如何|怎么|为什么|什么区别|方法|方案|思路|框架|实测|复盘|"
    r"对比|区别|原因|问题|经验|教程|观点|建议|适合|效果",
    re.I,
)
ATTACHMENT_PLACEHOLDER_TERMS = re.compile(
    r"^\[(?:图片|文件|语音|视频|image|file|voice|video)\]$|"
    r"\.(?:pptx?|pdf|docx?|xlsx?|key|pages)$",
    re.I,
)
GROUP_RECAP_TERMS = re.compile(
    r"^\s*(?:#{1,6}\s*)?"
    r"(?:(?:\d{4}|\d{1,2}[./月-]\d{1,2}日?|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})\s*)?"
    r"(?:(?:[\w·&+.-]{0,16})(?:微信群聊日报|群聊日报|群日报|群聊总结)|"
    r"(?:今日|昨日|本日|当天|每日).{0,6}(?:日报|总结|回顾)|"
    r"daily\s*(?:brief|digest|recap))"
    r"(?:\s|[:：|｜\-—]|$)",
    re.I,
)
PAID_BOOST_TERMS = re.compile(
    r"红包|加热|#?接龙|三连|四连|3连|4连|quote|引用|"
    r"截图.{0,8}(?:结算|发群|丢群)|统一结算|名额|(?:\d+(?:\.\d+)?)\s*(?:元|rmb)",
    re.I,
)
EXPLICIT_NON_DEAL_TERMS = re.compile(r"非商单|不是商单|并非商单|纯分享|非推广|无商业合作", re.I)
REACTIVATION_NEGATIVE_TERMS = re.compile(
    r"暂不(?:考虑|合作|投放|需要|推进)|暂时不(?:考虑|合作|投放|需要|推进)|不合作|不投(?:放)?|不考虑|"
    r"没预算|预算不够|效果不好|投放效果不|暂停(?:合作|投放|项目)|取消(?:合作|投放|项目)|下次吧|以后再说|"
    r"先不(?:合作|推进|投放|考虑)|不太合适|价格不合适|不匹配|不符合|婉拒|拒绝",
    re.I,
)
REACTIVATION_SUCCESS_TERMS = re.compile(
    r"已发布|发布了|发布链接|推文链接|文章链接|帖子链接|数据回传|回传数据|已经结算|结算完成|"
    r"已经付款|已付款|付款完成|已经付好|付好了|付好啦|已经打款|已打款|打款完成|收到款|款已到账|"
    r"已经到账|已到账|\bpaid\b|\bsettled\b|复盘|二次合作|继续合作",
    re.I,
)
REACTIVATION_DEFER_TERMS = re.compile(
    r"下一批|下批|下一轮|下轮|下次(?:我)?(?:再)?(?:跟你)?聊|下次合作|之后.*(?:联系|联络)|后面.*(?:联系|联络)|"
    r"有新(?:的)?(?:推广|投放|方案|项目).*(?:联系|联络|通知)|有合适.*(?:联系|联络|通知)|有需要.*(?:联系|联络)|会通知|"
    r"需要一段时间|还需要.*时间|晚一段时间|要等等|先等|再等等|等等.*(?:项目|机会|合作)|"
    r"已经推荐给团队|跟团队推荐过|下次我跟你聊",
    re.I,
)
REACTIVATION_VAGUE_WAIT_TERMS = re.compile(r"需要一段时间|还需要.*时间|晚一段时间|要等等|先等|再等等", re.I)
REACTIVATION_SETTLEMENT_CONTEXT_TERMS = re.compile(
    r"打款|付款|收款|结算|到账|内部流程|财务流程|invoice|payment|paid",
    re.I,
)
REACTIVATION_HANDOFF_TERMS = re.compile(
    r"暂时不负责|不再负责|已经离职|换(?:了)?负责人|(?:现在|后续|目前).{0,12}由.{1,20}负责|"
    r"拉.{0,12}(?:创始人|负责人|同事).{0,12}交接|交接.{0,12}(?:合作|项目|投放)",
    re.I,
)
REACTIVATION_COMMISSION_ONLY_TERMS = re.compile(
    r"纯佣|仅返佣|只有返佣|只做返佣|没有(?:基础)?稿费|无(?:基础)?稿费|没有保底|无保底|不保底",
    re.I,
)
REACTIVATION_THIRD_PARTY_CONTEXT = re.compile(
    r"另一个朋友|我(?:还有|有).{0,10}(?:朋友|博主|达人)|他(?:应该|可能|暂时)|她(?:应该|可能|暂时)",
    re.I,
)
REACTIVATION_NEGATIVE_HYPOTHETICAL_CONTEXT = re.compile(
    r"(?:如果|若).{0,20}(?:价格|报价|预算|费用).{0,20}(?:不合适|不匹配|不够|太高).{0,20}(?:可以|调整|再谈|商量)|"
    r"(?:价格|报价|预算|费用).{0,12}(?:不合适|不匹配|不够|太高).{0,12}(?:可以|能).{0,12}(?:调整|再谈|商量)",
    re.I,
)
REACTIVATION_ASSET_TERMS = re.compile(
    r"产品说明|产品介绍|素材|brief|卖点|邀请码|折扣码|专属码|链接|返佣|佣金|分成|CPA|CPS|affiliate|联盟",
    re.I,
)
DEFAULT_RADAR_DB = "~/.wechat-intelligence-hub/radar.db"
DEFAULT_COMPAT_DIR = "~/.wechat-intelligence-hub/compatibility"
DEFAULT_WECHAT_APP = "/Applications/WeChat.app"
URL_PATTERN = re.compile(r"https?://[^\s<>()\"'，。！？、；；]+", re.I)

AMOUNT_PATTERN = re.compile(
    r"(?:"
    r"[$￥¥]\s*\d+(?:,\d{3})*(?:\.\d+)?(?:\s*(?:USD|AUD|RMB|CNY|美元|澳币|人民币|刀|元))?"
    r"|"
    r"\d{3,}(?:,\d{3})*(?:\.\d+)?(?:\s*[-~到至]\s*\d{3,}(?:,\d{3})*(?:\.\d+)?)?\s*(?:USD|AUD|RMB|CNY|美元|澳币|人民币|刀|元)"
    r")",
    re.I,
)

TEXT_LINE_PATTERNS = [
    re.compile(r"^\[(?P<time>\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?P<sender>[^:：]+)[:：]\s*(?P<content>.*)$"),
    re.compile(r"^(?P<time>\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)\s+(?P<sender>[^:：]+)[:：]\s*(?P<content>.*)$"),
]


@dataclass
class Message:
    chat: str
    sender: str
    time: str
    content: str
    source_file: str


@dataclass
class Signal:
    chat: str
    sender: str
    time: str
    category: str
    stage: str
    priority: int
    amount: str
    content: str
    source_file: str


def normalize_time(value: str | None) -> str:
    # Keep the storage representation stable: historical message hashes include it.
    if not value:
        return ""
    value = str(value).replace("/", "-").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return value


def parse_time_for_filter(value: str | None) -> datetime | None:
    return parse_timestamp(display_timestamp(value))


def read_json_messages(path: Path) -> list[Message]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = extract_json_rows(raw)

    messages: list[Message] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        content = first_value(row, ["content", "text", "message", "msg", "body"])
        if not content:
            continue
        chat = first_value(row, ["chat", "chat_name", "room", "room_name", "contact", "talker", "session"]) or "未命名会话"
        sender = first_value(row, ["sender", "from", "speaker", "nickname", "user"]) or chat
        time = first_value(row, ["time", "create_time", "created_at", "datetime", "date"]) or ""
        messages.append(
            Message(
                chat=str(chat).strip(),
                sender=str(sender).strip(),
                time=normalize_time(str(time)),
                content=str(content).strip(),
                source_file=str(path),
            )
        )
    return messages


def extract_json_rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    if not isinstance(raw, dict):
        return []
    for key in ("messages", "sessions", "members", "data", "rows", "results", "items", "records"):
        value = raw.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            nested = extract_json_rows(value)
            if nested:
                return nested
    return []


def first_value(row: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if row.get(key):
            return row[key]
    return None


def read_text_messages(path: Path) -> list[Message]:
    messages: list[Message] = []
    fallback_chat = path.stem
    current_chat = fallback_chat
    self_names = {"我", "me", "Me", "ME", "自己"}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = None
        for pattern in TEXT_LINE_PATTERNS:
            parsed = pattern.match(line)
            if parsed:
                break
        if parsed:
            sender = parsed.group("sender").strip()
            content = parsed.group("content").strip()
            time = normalize_time(parsed.group("time"))
        else:
            sender = "未知"
            content = line
            time = ""
        if sender not in self_names and sender != "未知":
            current_chat = sender
        messages.append(
            Message(
                chat=current_chat,
                sender=sender,
                time=time,
                content=content,
                source_file=str(path),
            )
        )
    return messages


def load_messages(paths: list[Path]) -> list[Message]:
    messages: list[Message] = []
    for path in paths:
        if path.suffix.lower() == ".json":
            messages.extend(read_json_messages(path))
        else:
            messages.extend(read_text_messages(path))
    return messages


def load_watchlist(path_value: str | None) -> list[str]:
    if not path_value:
        return []
    path = Path(path_value).expanduser()
    if not path.exists():
        raise SystemExit(f"找不到合作方名单：{path}")
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.append(line)
    return names


def existing_optional_list(path_value: str) -> str | None:
    """Use a conventional local list when present without making it mandatory."""
    return path_value if Path(path_value).expanduser().is_file() else None


def filter_by_lists(messages: list[Message], watchlist: list[str], source_list: list[str]) -> list[Message]:
    if not watchlist and not source_list:
        return messages
    watch_lowered = [name.lower() for name in watchlist]
    source_lowered = [name.lower() for name in source_list]
    filtered: list[Message] = []
    for message in messages:
        haystack = f"{message.chat} {message.sender}".lower()
        chat = message.chat.lower()
        matched_watch = any(name in haystack for name in watch_lowered)
        matched_source = any(name in chat for name in source_lowered)
        if matched_watch or matched_source:
            filtered.append(message)
    return filtered


def filter_by_exclude_list(messages: list[Message], exclude_list: list[str]) -> list[Message]:
    if not exclude_list:
        return messages
    lowered = [name.lower() for name in exclude_list]
    filtered: list[Message] = []
    for message in messages:
        haystack = f"{message.chat} {message.sender}".lower()
        if any(name in haystack for name in lowered):
            continue
        filtered.append(message)
    return filtered


def filter_by_time(messages: list[Message], since: str | None, until: str | None) -> list[Message]:
    start = parse_time_for_filter(since)
    end = parse_time_for_filter(until)
    if not start and not end:
        return messages
    filtered: list[Message] = []
    for message in messages:
        message_time = parse_time_for_filter(message.time)
        if not message_time:
            filtered.append(message)
            continue
        if start and message_time < start:
            continue
        if end and message_time > end:
            continue
        filtered.append(message)
    return filtered


def analyze_messages(messages: list[Message], out_dir: Path) -> tuple[list[Signal], list[dict[str, str]]]:
    signals = extract_signals(messages)
    deals = group_deals(signals)
    out_dir.mkdir(parents=True, exist_ok=True)

    write_csv(
        out_dir / "signals.csv",
        [asdict(signal) for signal in signals],
        ["chat", "sender", "time", "category", "stage", "priority", "amount", "content", "source_file"],
    )
    write_csv(
        out_dir / "deals.csv",
        deals,
        ["品牌/聊天对象", "当前阶段", "下一步动作", "优先级", "最后信号时间", "报价/预算", "证据条数", "证据摘要", "来源聊天"],
    )
    (out_dir / "signals.json").write_text(
        json.dumps({"messages": len(messages), "signals": [asdict(signal) for signal in signals], "deals": deals}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_digest(out_dir / "daily_digest.md", deals, signals)
    return signals, deals


def extract_signals(messages: list[Message]) -> list[Signal]:
    signals: list[Signal] = []
    compiled = [(category, stage, priority, re.compile(pattern, re.I)) for category, stage, priority, pattern in SIGNAL_RULES]
    for message in messages:
        for category, stage, priority, pattern in compiled:
            if not pattern.search(message.content):
                continue
            if category == "结算":
                category, stage, priority = settlement_signal(message.content)
            amount = " / ".join(clean_amounts(AMOUNT_PATTERN.findall(message.content)))
            signals.append(
                Signal(
                    chat=message.chat,
                    sender=message.sender,
                    time=message.time,
                    category=category,
                    stage=stage,
                    priority=priority,
                    amount=amount,
                    content=message.content,
                    source_file=message.source_file,
                )
            )
    return signals


def clean_amounts(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        value = re.sub(r"\s+", " ", value).strip()
        if value and value not in cleaned and re.search(r"\d", value):
            cleaned.append(value)
    return cleaned


def stage_rank(stage: str) -> int:
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return 0


def group_deals(signals: list[Signal]) -> list[dict[str, str]]:
    grouped: dict[str, list[Signal]] = {}
    for signal in signals:
        grouped.setdefault(signal.chat, []).append(signal)

    deals: list[dict[str, str]] = []
    for chat, rows in grouped.items():
        rows = sorted(rows, key=lambda row: row.time or "", reverse=True)
        strongest_stage = max(rows, key=lambda row: stage_rank(row.stage)).stage
        max_priority = max(row.priority for row in rows)
        amounts = unique_join(row.amount for row in rows if row.amount)
        latest = rows[0]
        next_action = infer_next_action(strongest_stage)
        evidence = " | ".join(unique_items(shorten(row.content) for row in rows)[:3])
        deals.append(
            {
                "品牌/聊天对象": chat,
                "当前阶段": strongest_stage,
                "下一步动作": next_action,
                "优先级": str(max_priority),
                "最后信号时间": latest.time,
                "报价/预算": amounts,
                "证据条数": str(len(rows)),
                "证据摘要": evidence,
                "来源聊天": chat,
            }
        )
    return sorted(deals, key=lambda row: (int(row["优先级"]), row["最后信号时间"]), reverse=True)


def unique_join(values: Iterable[str]) -> str:
    return " / ".join(unique_items(values))


def unique_items(values: Iterable[str]) -> list[str]:
    items: list[str] = []
    for value in values:
        if value and value not in items:
            items.append(value)
    return items


def shorten(text: str, limit: int = 72) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def infer_next_action(stage: str) -> str:
    return {
        "新线索": "判断是否值得接，补问预算/目标/平台",
        "待回复": "尽快回复，避免线索冷掉",
        "待报价": "发报价和可选档位",
        "待 brief": "催 brief、素材、发布时间和审核要求",
        "待确认报价/排期": "确认价格、排期、交付形式和结算方式",
        "待创作": "整理 brief，进入初稿",
        "待品牌审核": "跟进修改反馈或确认过稿",
        "待发布": "确认发布时间和发布素材",
        "已发布待结算": "跟进数据回传、发票/收款",
        "已发布待数据跟进": "落实补量、Quote/KOL 转发，并在统计截止前回传新增数据",
        "已收款": "归档复盘，保温复购",
    }.get(stage, "人工检查")


def infer_group_action(topics: list[str], opportunity_messages: list[Message], unique_opportunity_count: int) -> str:
    if unique_opportunity_count <= 0:
        return "低优先级，不用看"
    text = "\n".join(message.content for message in opportunity_messages)
    if re.search(r"求推荐|谁想|想接|可接|招募|内推|KOL|博主|达人|预算|报价|有偿|BD|远程全职|兼职|外包", text, re.I):
        return "可能和你有关，优先点开原群确认是否能接/能被推荐"
    if re.search(r"小红书|公众号|YouTube|视频|图文|直发|转发|转推|quote|引用|三连|四连|加热|接龙|红包", text, re.I):
        return "疑似商单加热；若同链接跨群出现，优先追发帖博主/中间人问品牌和下一批名额"
    if "AI 产品/模型" in topics or "工具/产品推荐" in topics:
        return "可当选题/产品线索扫一眼，暂不需要动作"
    return "只作为情报记录，暂不需要处理"


def is_deal_opportunity(message: Message) -> bool:
    text = message.content
    if EXPLICIT_NON_DEAL_TERMS.search(text):
        return False
    if DEAL_OPPORTUNITY_TERMS.search(text) or PAID_BOOST_TERMS.search(text):
        return True
    return bool(AMOUNT_PATTERN.search(text) and DIRECT_DEAL_TERMS.search(text))


def is_training_project_opportunity(message: Message) -> bool:
    return bool(TRAINING_PROJECT_TERMS.search(message.content))


def adjacent_attachment_note(messages: list[Message], focus_messages: list[Message], radius: int = 2) -> str:
    if not focus_messages:
        return ""
    focus_ids = {id(message) for message in focus_messages}
    attachment_types: set[str] = set()
    for index, message in enumerate(messages):
        if id(message) not in focus_ids:
            continue
        start = max(0, index - radius)
        end = min(len(messages), index + radius + 1)
        for nearby in messages[start:end]:
            content = nearby.content.strip()
            if not ATTACHMENT_PLACEHOLDER_TERMS.search(content):
                continue
            if "图片" in content or "image" in content.lower():
                attachment_types.add("图片")
            elif "语音" in content or "voice" in content.lower():
                attachment_types.add("语音")
            elif "视频" in content or "video" in content.lower():
                attachment_types.add("视频")
            else:
                attachment_types.add("文件")
    if not attachment_types:
        return ""
    return f"附近有{'、'.join(sorted(attachment_types))}，需展开原始附件核验完整需求"


JOINED_CHANNEL_INVITE_TERMS = re.compile(r"进.{0,8}群|加入.{0,8}群|扫码|二维码|通过审核|报名接单", re.I)


def is_joined_channel_repeat_invite(message: Message, joined_channel_markers: list[str]) -> bool:
    if not joined_channel_markers or not JOINED_CHANNEL_INVITE_TERMS.search(message.content):
        return False
    haystack = f"{message.chat} {message.sender} {message.content}".lower()
    return any(marker.lower() in haystack for marker in joined_channel_markers)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_digest(path: Path, deals: list[dict[str, str]], signals: list[Signal]) -> None:
    priority_deals = [deal for deal in deals if int(deal["优先级"]) >= 4]
    lines = [
        "# 微信个人情报库日报",
        "",
        f"- 识别商单信号：{len(signals)} 条",
        f"- 关联聊天对象：{len(deals)} 个",
        f"- 高优先级对象：{len(priority_deals)} 个",
        "",
        "## 今天优先处理",
        "",
    ]
    if not priority_deals:
        lines.append("暂无高优先级信号。")
    for deal in priority_deals[:12]:
        lines.append(f"- **{deal['品牌/聊天对象']}**：{deal['当前阶段']}，{deal['下一步动作']}。证据：{deal['证据摘要']}")

    lines.extend(["", "## 全部线索", ""])
    for deal in deals:
        amount = f"，预算/报价：{deal['报价/预算']}" if deal["报价/预算"] else ""
        lines.append(f"- {deal['品牌/聊天对象']}：{deal['当前阶段']}{amount}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_handoff_relations(path_value: str | None) -> list[dict[str, str]]:
    if not path_value:
        return []
    rows = read_csv_rows(Path(path_value).expanduser())
    relations: list[dict[str, str]] = []
    for row in rows:
        status = str(row.get("状态") or "有效").strip()
        if status in {"停用", "无效", "已删除"}:
            continue
        old_owner = str(row.get("原负责人") or "").strip()
        new_owner = str(row.get("新负责人") or "").strip()
        group_id = str(row.get("共同群ID") or "").strip()
        if not old_owner or not new_owner or not group_id:
            continue
        relations.append(
            {
                "项目": str(row.get("项目") or "").strip(),
                "原负责人": old_owner,
                "新负责人": new_owner,
                "共同群ID": group_id,
                "共同群名": str(row.get("共同群名") or group_id).strip(),
                "交接确认时间": str(row.get("交接确认时间") or "").strip(),
                "状态": status,
            }
        )
    return relations


def target_handoff_contexts(target: dict[str, Any], relations: list[dict[str, str]]) -> list[dict[str, str]]:
    names = {
        str(name).strip().lower()
        for name in [target.get("display_name"), *(target.get("names") or [])]
        if str(name or "").strip()
    }
    contexts: list[dict[str, str]] = []
    for relation in relations:
        old_owner = relation["原负责人"].lower()
        new_owner = relation["新负责人"].lower()
        if old_owner in names:
            contexts.append({**relation, "角色": "原负责人"})
        elif new_owner in names:
            contexts.append({**relation, "角色": "新负责人"})
    return contexts


def handoff_relation_summary(contexts: list[dict[str, str]]) -> str:
    summaries = []
    for context in contexts:
        project = f"{context['项目']}：" if context["项目"] else ""
        group = context["共同群名"] or context["共同群ID"]
        summaries.append(f"{project}{context['原负责人']} → {context['新负责人']}；共同讨论组：{group}")
    return " | ".join(unique_items(summaries))


def load_contact_label_index(path_value: str | None) -> dict[str, set[str]]:
    if not path_value:
        return {}
    rows = read_csv_rows(Path(path_value).expanduser())
    index: dict[str, set[str]] = {}
    for row in rows:
        label = (row.get("标签") or "").strip()
        if not label:
            continue
        for key in ("显示名", "备注", "昵称", "微信号/alias", "username"):
            name = (row.get(key) or "").strip()
            if not name:
                continue
            index.setdefault(name.lower(), set()).add(label)
    return index


def matched_contact_labels(chat: str, sender: str, label_index: dict[str, set[str]]) -> list[str]:
    if not label_index:
        return []
    haystack = f"{chat} {sender}".lower()
    labels: set[str] = set()
    for name, row_labels in label_index.items():
        if name and name in haystack:
            labels.update(row_labels)
    return sorted(labels)


def signal_score(signal: Signal, labels: list[str]) -> int:
    text = f"{signal.chat} {signal.sender} {signal.content}"
    score = signal.priority * 10 + stage_rank(signal.stage) * 3
    if set(labels) & set(configured_labels("commercial")):
        score += 32
    if set(labels) & set(configured_labels("creator")):
        score += 16
    if signal.stage in ACTIVE_STAGES:
        score += 24
    if signal.stage in SETTLEMENT_STAGES:
        score += 14
    if signal.amount:
        score += 18
    if DIRECT_DEAL_TERMS.search(text):
        score += 12
    if RESOURCE_TERMS.search(text):
        score += 8
    if LOW_SIGNAL_CHAT_TERMS.search(signal.chat):
        score -= 14
    return max(score, 0)


def classify_signal(signal: Signal, labels: list[str]) -> str:
    text = f"{signal.chat} {signal.sender} {signal.content}"
    low_signal_chat = bool(LOW_SIGNAL_CHAT_TERMS.search(signal.chat))
    if signal.stage in SETTLEMENT_STAGES:
        return "待结算/复盘"
    if set(labels) & set(configured_labels("commercial")):
        return "商单推进"
    if set(labels) & set(configured_labels("creator")) and (low_signal_chat or "群" in signal.chat or RESOURCE_TERMS.search(text)):
        return "二跳资源机会"
    if signal.stage in ACTIVE_STAGES:
        return "商单推进"
    if set(labels) & set(configured_labels("creator")) or RESOURCE_TERMS.search(text):
        return "二跳资源机会"
    if low_signal_chat:
        return "低优先级噪音"
    return "暂缓观察"


def summarize_priorities(signals: list[Signal], label_index: dict[str, set[str]]) -> list[dict[str, str]]:
    grouped: dict[tuple[str, str], list[tuple[Signal, list[str], int]]] = {}
    for signal in signals:
        labels = matched_contact_labels(signal.chat, signal.sender, label_index)
        bucket = classify_signal(signal, labels)
        grouped.setdefault((bucket, signal.chat), []).append((signal, labels, signal_score(signal, labels)))

    rows: list[dict[str, str]] = []
    for (bucket, chat), items in grouped.items():
        items = sorted(items, key=lambda item: (item[2], item[0].time or ""), reverse=True)
        signal_rows = [item[0] for item in items]
        labels = sorted({label for _, item_labels, _ in items for label in item_labels})
        strongest_stage = max(signal_rows, key=lambda row: stage_rank(row.stage)).stage
        latest = max(signal_rows, key=lambda row: row.time or "")
        amounts = unique_join(row.amount for row in signal_rows if row.amount)
        evidence = " | ".join(unique_items(shorten(row.content, 86) for row in signal_rows)[:3])
        total_score = min(sum(item[2] for item in items[:6]), 999)
        rows.append(
            {
                "分组": bucket,
                "分数": str(total_score),
                "聊天对象": chat,
                "当前阶段": strongest_stage,
                "下一步动作": infer_next_action(strongest_stage),
                "最后信号时间": latest.time,
                "报价/预算": amounts,
                "匹配标签": "、".join(labels),
                "证据条数": str(len(signal_rows)),
                "证据摘要": evidence,
            }
        )

    bucket_order = {"商单推进": 0, "二跳资源机会": 1, "待结算/复盘": 2, "暂缓观察": 3, "低优先级噪音": 4}
    return sorted(rows, key=lambda row: (bucket_order.get(row["分组"], 9), -int(row["分数"]), row["最后信号时间"]))


def write_priority_digest(path: Path, rows: list[dict[str, str]]) -> None:
    section_limits = {
        "商单推进": 20,
        "二跳资源机会": 18,
        "待结算/复盘": 14,
        "暂缓观察": 10,
        "低优先级噪音": 8,
    }
    lines = [
        "# 微信商单机会工作台",
        "",
        "这份是按你的工作流重排后的结果：先看能推进现金流的，再看二跳资源，最后看噪音。",
        "",
    ]
    for section, limit in section_limits.items():
        section_rows = [row for row in rows if row["分组"] == section]
        lines.extend([f"## {section}", ""])
        if not section_rows:
            lines.extend(["暂无。", ""])
            continue
        for row in section_rows[:limit]:
            amount = f"，预算/报价：{row['报价/预算']}" if row["报价/预算"] else ""
            labels = f"，标签：{row['匹配标签']}" if row["匹配标签"] else ""
            lines.append(
                f"- **{row['聊天对象']}**（{row['当前阶段']}，分数 {row['分数']}{labels}{amount}）："
                f"{row['下一步动作']}。证据：{row['证据摘要']}"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def prioritize(args: argparse.Namespace) -> None:
    signals_path = Path(args.signals).expanduser()
    if not signals_path.exists():
        raise SystemExit(f"找不到 signals.json：{signals_path}")
    raw = json.loads(signals_path.read_text(encoding="utf-8"))
    signals = [Signal(**row) for row in raw.get("signals", []) if isinstance(row, dict)]
    label_index = load_contact_label_index(args.contacts)
    rows = summarize_priorities(signals, label_index)

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(
        out_dir / "prioritized_deals.csv",
        rows,
        ["分组", "分数", "聊天对象", "当前阶段", "下一步动作", "最后信号时间", "报价/预算", "匹配标签", "证据条数", "证据摘要"],
    )
    write_priority_digest(out_dir / "prioritized_digest.md", rows)
    print(f"读取信号：{len(signals)} 条")
    print(f"聚合对象：{len(rows)} 个")
    print(f"输出目录：{out_dir}")


def daily(args: argparse.Namespace) -> None:
    contacts_dir = Path(args.contacts_dir).expanduser()
    scan_out = Path(args.scan_out).expanduser()
    final_out = Path(args.out).expanduser()
    labels = args.label or configured_labels("priority")

    print("步骤 1/3：同步微信标签联系人")
    wechat_labels(
        argparse.Namespace(
            wechat_cli=args.wechat_cli,
            label=labels,
            out=str(contacts_dir),
        )
    )

    print("\n步骤 2/3：扫描本地微信聊天")
    wechat_scan(
        argparse.Namespace(
            wechat_cli=args.wechat_cli,
            keyword=args.keyword,
            chat=args.chat,
            watchlist=str(contacts_dir / "重点联系人名单.txt"),
            source_list=args.source_list,
            exclude_list=args.exclude_list,
            since=args.since,
            until=args.until,
            limit=args.limit,
            max_pages=args.max_pages,
            max_text_chars=args.max_text_chars,
            out=str(scan_out),
            db=args.db,
            no_db=args.no_db,
        )
    )

    print("\n步骤 3/3：生成个人商单机会工作台")
    prioritize(
        argparse.Namespace(
            signals=str(scan_out / "signals.json"),
            contacts=str(contacts_dir / "微信标签联系人.csv"),
            out=str(final_out),
        )
    )


def local_timestamp(value: str | None) -> int | None:
    parsed = parse_time_for_filter(value)
    if not parsed:
        return None
    return int(parsed.timestamp())


def resolve_time_window(since: str | None, until: str | None, hours: float = 24) -> tuple[str, str]:
    end = parse_time_for_filter(until) if until else datetime.now()
    if since:
        start = parse_time_for_filter(since)
        if not start:
            raise SystemExit(f"无法识别开始时间：{since}")
    else:
        if hours <= 0:
            raise SystemExit("时间窗口小时数必须大于 0")
        start = end - timedelta(hours=hours)
    if start > end:
        raise SystemExit("开始时间不能晚于结束时间")
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def fetch_group_sessions(wechat_cli: Path, limit: int, since: str | None) -> list[dict[str, Any]]:
    command = [
        str(wechat_cli),
        "sessions",
        "--type-filter",
        "group",
        "--limit",
        str(limit),
    ]
    return extract_json_rows(run_json_command(command))


def fetch_group_members(wechat_cli: Path, session: dict[str, Any], limit: int = 2000) -> list[dict[str, Any]]:
    chatroom_id = str(session.get("username") or "").strip()
    if not chatroom_id:
        return []
    command = [
        str(wechat_cli),
        "members",
        chatroom_id,
        "--limit",
        str(limit),
        "--strict-read-only",
    ]
    return extract_json_rows(run_json_command(command))


def fetch_sessions(wechat_cli: Path, limit: int, type_filter: str | None = None) -> list[dict[str, Any]]:
    command = [str(wechat_cli), "sessions", "--limit", str(limit)]
    if type_filter and type_filter != "all":
        command.extend(["--type-filter", type_filter])
    return extract_json_rows(run_json_command(command))


def resolve_chat_session(wechat_cli: Path, chat: str, type_filter: str | None = None) -> dict[str, Any]:
    command = [str(wechat_cli), "resolve-chat", chat]
    if type_filter and type_filter != "all":
        command.extend(["--type-filter", type_filter])
    raw = run_json_command(command)
    data = raw.get("data") if isinstance(raw, dict) else {}
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(candidates, list):
        candidates = extract_json_rows(raw)
    if not candidates:
        raise SystemExit(f"找不到聊天对象：{chat}")
    first = candidates[0]
    username = str(first_value(first, ["username", "talker", "chatroom_id"]) or "").strip()
    display_name = str(first_value(first, ["display_name", "nick_name", "remark", "name"]) or chat).strip()
    if not username:
        raise SystemExit(f"聊天对象缺少 username：{chat}")
    return {
        "display_name": display_name or chat,
        "username": username,
        "chat_type": str(first.get("chat_type") or type_filter or ""),
    }


def filter_sessions_by_exclude_list(sessions: list[dict[str, Any]], exclude_list: list[str]) -> list[dict[str, Any]]:
    if not exclude_list:
        return sessions
    lowered = [item.lower() for item in exclude_list]
    filtered: list[dict[str, Any]] = []
    for session in sessions:
        haystack = f"{session.get('display_name') or ''} {session.get('username') or ''} {session.get('summary') or ''}".lower()
        if any(item in haystack for item in lowered):
            continue
        filtered.append(session)
    return filtered


def timeline_messages_for_group(wechat_cli: Path, session: dict[str, Any], since: str | None, until: str | None, limit: int) -> list[Message]:
    session.pop("_timeline_coverage", None)
    chat_name = str(session.get("display_name") or session.get("username") or "").strip()
    talker = str(session.get("username") or "").strip()
    if not talker:
        return []
    messages: list[Message] = []
    offset = 0
    page_size = min(max(limit, 1), 100)
    while len(messages) < limit:
        command = [
            str(wechat_cli),
            "timeline",
            talker,
            "--limit",
            str(min(page_size, limit - len(messages))),
            "--offset",
            str(offset),
            "--display-order",
            "asc",
            "--include-media-paths",
            "false",
        ]
        if since:
            command.extend(["--since", since])
        if until:
            command.extend(["--before", until])
        raw = run_json_command(command)
        rows = extract_json_rows(raw)
        for row in rows:
            text = first_value(row, ["text", "content_summary", "display"])
            if isinstance(text, dict):
                text = first_value(text, ["text", "title", "summary"]) or ""
            if not text:
                kind = str(row.get("kind") or row.get("kind_name") or "消息")
                text = f"[{kind}]"
            sender = str(first_value(row, ["sender", "sender_display_name", "sender_wxid"]) or "").strip()
            time = str(first_value(row, ["time", "time_iso", "create_time"]) or "").strip()
            if time.isdigit():
                time = datetime.fromtimestamp(int(time)).strftime("%Y-%m-%d %H:%M:%S")
            messages.append(
                Message(
                    chat=chat_name,
                    sender=sender or chat_name,
                    time=normalize_time(time),
                    content=str(text).strip(),
                    source_file=f"wechat-cli:timeline:{talker}",
                )
            )
        query = extract_query(raw)
        session["_timeline_coverage"] = {
            "read": len(messages),
            "limit": limit,
            "incomplete": bool(query.get("has_more")),
            "next_offset": query.get("next_offset"),
        }
        if not rows or not query.get("has_more"):
            break
        next_offset = query.get("next_offset")
        if not isinstance(next_offset, int) or next_offset <= offset:
            break
        offset = next_offset
    return sorted(dedupe_messages(messages), key=lambda message: message.time or "")


def group_topics(messages: list[Message]) -> list[str]:
    scores: Counter[str] = Counter()
    for message in messages:
        if is_group_recap_message(message):
            continue
        text = f"{message.sender} {message.content}"
        for topic, pattern in GROUP_TOPIC_RULES:
            if re.search(pattern, text, re.I):
                scores[topic] += 1
    return [topic for topic, _ in scores.most_common(5)]


def message_topic(message: Message) -> str:
    text = f"{message.sender} {message.content}"
    for topic, pattern in GROUP_TOPIC_RULES:
        if re.search(pattern, text, re.I):
            return topic
    return "日常闲聊/信息同步"


def is_group_recap_message(message: Message) -> bool:
    content = message.content.strip()
    if GROUP_RECAP_TERMS.match(content):
        return True
    recap_senders = {sender.casefold() for sender in configured_group_recap_senders(message.chat)}
    recap_windows = configured_group_recap_time_windows(message.chat)
    return bool(
        recap_senders
        and message.sender.strip().casefold() in recap_senders
        and ATTACHMENT_PLACEHOLDER_TERMS.fullmatch(content)
        and message_time_in_windows(message.time, recap_windows)
    )


def group_recap_entries(messages: list[Message]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for message in sorted(messages, key=lambda item: item.time or ""):
        if not is_group_recap_message(message):
            continue
        content = message.content.strip()
        attachment_match = ATTACHMENT_PLACEHOLDER_TERMS.fullmatch(content)
        recap_format = content.strip("[]") if attachment_match else "文字"
        summary = "图片日报，内容需按需读取原图" if attachment_match else shorten(content.splitlines()[0], 100)
        entries.append(
            {
                "时间": message.time,
                "发言人": message.sender,
                "形式": recap_format,
                "摘要": summary,
            }
        )
    return entries


def load_group_recaps(row: dict[str, str]) -> list[dict[str, str]]:
    try:
        value = json.loads(row.get("群内已有日报") or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def format_message_time(value: str) -> str:
    parsed = parse_time_for_filter(value)
    if not parsed:
        return value[:5] if value else ""
    return parsed.strftime("%H:%M")


def build_group_timeline(messages: list[Message], max_segments: int = 8) -> str:
    messages = [message for message in messages if not is_group_recap_message(message)]
    if not messages:
        return ""
    sorted_messages = sorted(messages, key=lambda message: message.time or "")
    segments: list[dict[str, Any]] = []
    for message in sorted_messages:
        topic = message_topic(message)
        parsed_time = parse_time_for_filter(message.time)
        should_start = True
        if segments:
            previous = segments[-1]
            previous_time = parse_time_for_filter(previous["messages"][-1].time)
            close_in_time = bool(parsed_time and previous_time and (parsed_time - previous_time).total_seconds() <= 30 * 60)
            should_start = not (previous["topic"] == topic and close_in_time)
        if should_start:
            segments.append({"topic": topic, "messages": [message]})
        else:
            segments[-1]["messages"].append(message)

    compact_segments: list[str] = []
    for segment in segments[:max_segments]:
        rows: list[Message] = segment["messages"]
        start = format_message_time(rows[0].time)
        end = format_message_time(rows[-1].time)
        speakers = "、".join(name for name, _ in Counter(row.sender for row in rows).most_common(3))
        snippets = compact_key_messages(rows, limit=2)
        if not snippets:
            snippets = [f"{rows[0].sender}：{shorten(rows[0].content, 72)}"]
        time_label = start if start == end else f"{start}-{end}"
        compact_segments.append(f"{time_label} {segment['topic']}（{len(rows)}条，{speakers}）：{' / '.join(snippets)}")
    if len(segments) > max_segments:
        compact_segments.append(f"另有 {len(segments) - max_segments} 段低优先级讨论。")
    return "；".join(compact_segments)


def message_topics(message: Message) -> list[str]:
    text = f"{message.sender} {message.content}"
    topics = [topic for topic, pattern in GROUP_TOPIC_RULES if re.search(pattern, text, re.I)]
    return topics[:3] or ["日常闲聊/信息同步"]


BOOST_COORDINATION_TERMS = re.compile(
    r"红包|求加热|帮忙加热|#?接龙|三连|四连|3连|4连|五连|5连|"
    r"点赞|评论|收藏|转发|转推|截图结算|统一结算|加热了|已加热",
    re.I,
)


def is_boost_coordination_message(content: str) -> bool:
    """Keep paid-boost traces in link radar, not in the group digest."""
    if not BOOST_COORDINATION_TERMS.search(content or ""):
        return False
    substantive = bool(
        GROUP_DIGEST_ACTIONABLE_DEAL_TERMS.search(content)
        or GROUP_DIGEST_ACTIONABLE_TRAINING_TERMS.search(content)
        or GROUP_DIGEST_ACTIONABLE_PROJECT_TERMS.search(content)
        or GROUP_DIGEST_ACTIONABLE_EVENT_TERMS.search(content)
        or GROUP_DIGEST_ACTIONABLE_JOB_TERMS.search(content)
    )
    return not substantive


def discussion_signal_categories(message: Message, project_collaboration: bool = False) -> list[str]:
    content = message.content
    categories: list[str] = []
    if GROUP_DIGEST_ACTIONABLE_DEAL_TERMS.search(content) or (
        project_collaboration and PIPELINE_CONTEXT_TERMS.search(content)
    ):
        categories.append("商单")
    if GROUP_DIGEST_ACTIONABLE_TRAINING_TERMS.search(content):
        categories.append("培训")
    if GROUP_DIGEST_ACTIONABLE_PROJECT_TERMS.search(content):
        categories.append("项目合作")
    event_signal = bool(GROUP_DIGEST_ACTIONABLE_EVENT_TERMS.search(content))
    if event_signal:
        categories.append("活动")
    explicit_job_signal = bool(re.search(r"招聘|急招|内推|岗位|实习|兼职|外包|接单", content, re.I))
    if GROUP_DIGEST_ACTIONABLE_JOB_TERMS.search(content) and (not event_signal or explicit_job_signal):
        categories.append("招聘/外包")
    if GROUP_DIGEST_ACTIONABLE_MONEY_TERMS.search(content):
        categories.append("赚钱/奖励")
    return unique_items(categories)


def _discussion_thread_attention(
    categories: list[str],
    messages: list[Message],
    speaker_count: int,
) -> tuple[str, str]:
    text = " ".join(message.content for message in messages)
    details: list[str] = []
    if categories:
        details.append(f"出现明确的{'/'.join(categories)}信号")
        if AMOUNT_PATTERN.search(text):
            details.append("涉及金额或预算")
        if DISCUSSION_DEADLINE_TERMS.search(text):
            details.append("涉及时间或截止节点")
    elif AMOUNT_PATTERN.search(text):
        details.append("出现报价或成本讨论，可作为市场参考")
    elif DISCUSSION_INFORMATION_TERMS.search(text):
        details.append("出现产品、项目或规则的新变化")
    if details and re.search(r"\[(?:图片|文件|语音|视频)\]", text):
        details.append("完整信息可能在附件中")
    if details:
        level = "立即处理" if categories and (
            DISCUSSION_DEADLINE_TERMS.search(text) or AMOUNT_PATTERN.search(text)
        ) else "值得关注"
        return level, "；".join(details)
    if len(messages) >= 8 and speaker_count >= 3 and DISCUSSION_SUBSTANCE_TERMS.search(text):
        return "背景动态", "群内讨论较活跃，但尚未检出可核查的新信息、明确结论或行动信号"
    return "背景动态", "作为当前群聊主题背景保留"


def compact_evidence_messages(messages: list[Message], limit: int = 4) -> list[dict[str, str]]:
    scored: list[tuple[int, Message]] = []
    for message in messages:
        content = message.content.strip()
        if (
            not content
            or is_noise_evidence(content)
            or ATTACHMENT_PLACEHOLDER_TERMS.fullmatch(content)
            or is_group_recap_message(message)
        ):
            continue
        score = min(len(content), 180)
        if discussion_signal_categories(message):
            score += 180
        if DISCUSSION_INFORMATION_TERMS.search(content):
            score += 100
        if DISCUSSION_SUBSTANCE_TERMS.search(content):
            score += 60
        if AMOUNT_PATTERN.search(content) or DISCUSSION_DEADLINE_TERMS.search(content):
            score += 80
        scored.append((score, message))
    scored.sort(key=lambda item: (item[0], item[1].time), reverse=True)
    selected: list[Message] = []
    seen: set[str] = set()
    for _, message in scored:
        key = evidence_dedupe_key(message.content)
        if key in seen:
            continue
        seen.add(key)
        selected.append(message)
        if len(selected) >= limit:
            break
    selected.sort(key=lambda message: message.time or "")
    return [
        {
            "时间": message.time,
            "发言人": message.sender,
            "内容": shorten(message.content, 320),
        }
        for message in selected
    ]


def build_group_discussion_threads(
    messages: list[Message],
    *,
    project_collaboration: bool = False,
    max_threads: int = 12,
) -> list[dict[str, Any]]:
    cleaned = [
        message
        for message in sorted(dedupe_messages(messages), key=lambda item: item.time or "")
        if message.sender != "系统"
        and not is_noise_evidence(message.content)
        and not is_low_value_group_message(message.content)
        and not is_boost_coordination_message(message.content)
        and not is_group_recap_message(message)
        and not re.search(r"当前版本不支持展示|请升级至最新版本|撤回了一条消息", message.content)
    ]
    if not cleaned:
        return []

    time_sessions: list[list[Message]] = []
    for message in cleaned:
        if not time_sessions:
            time_sessions.append([message])
            continue
        previous = time_sessions[-1][-1]
        previous_time = parse_time_for_filter(previous.time)
        current_time = parse_time_for_filter(message.time)
        gap_minutes = (
            (current_time - previous_time).total_seconds() / 60
            if current_time and previous_time
            else 0
        )
        if gap_minutes > 45:
            time_sessions.append([message])
        else:
            time_sessions[-1].append(message)

    segments: list[list[Message]] = []
    for session in time_sessions:
        primary_topics = [message_topics(message)[0] for message in session]
        assigned_topics: list[str] = []
        for index, topic in enumerate(primary_topics):
            if topic != "日常闲聊/信息同步":
                assigned_topics.append(topic)
                continue
            replacement = ""
            for neighbor in (index - 1, index + 1):
                if neighbor < 0 or neighbor >= len(session):
                    continue
                neighbor_topic = primary_topics[neighbor]
                if neighbor_topic == "日常闲聊/信息同步":
                    continue
                here = parse_time_for_filter(session[index].time)
                there = parse_time_for_filter(session[neighbor].time)
                if here and there and abs((here - there).total_seconds()) <= 10 * 60:
                    replacement = neighbor_topic
                    break
            assigned_topics.append(replacement or topic)

        topic_groups: dict[str, list[Message]] = {}
        for message, topic in zip(session, assigned_topics):
            topic_groups.setdefault(topic, []).append(message)
        segments.extend(topic_groups.values())
    segments.sort(key=lambda rows: rows[0].time or "")

    threads: list[dict[str, Any]] = []
    for rows in segments:
        topic_scores: Counter[str] = Counter()
        categories: list[str] = []
        for message in rows:
            topic_scores.update(message_topics(message))
            categories.extend(discussion_signal_categories(message, project_collaboration))
        categories = unique_items(categories)
        topics = [
            topic
            for topic, _ in topic_scores.most_common(4)
            if topic != "日常闲聊/信息同步"
        ][:3]
        if not topics:
            topics = ["日常闲聊/信息同步"]
        representative_rows = rows
        if categories:
            representative_rows = [
                row
                for row in rows
                if discussion_signal_categories(row, project_collaboration)
            ] or rows
        elif any(AMOUNT_PATTERN.search(row.content) for row in rows):
            representative_rows = [row for row in rows if AMOUNT_PATTERN.search(row.content)]
        elif any(DISCUSSION_INFORMATION_TERMS.search(row.content) for row in rows):
            representative_rows = [row for row in rows if DISCUSSION_INFORMATION_TERMS.search(row.content)]
        representatives = compact_key_messages(representative_rows, limit=2)
        speakers = [name for name, _ in Counter(row.sender for row in rows).most_common(4)]
        speaker_count = len(set(row.sender for row in rows))
        attention_level, attention = _discussion_thread_attention(categories, rows, speaker_count)
        evidence = "；".join(representatives) if representatives else f"{rows[0].sender}：{shorten(rows[0].content, 96)}"
        evidence = re.sub(r"https?://\S+", "[链接见聚合区]", evidence)
        score = (
            len(categories) * 100
            + (35 if any(AMOUNT_PATTERN.search(row.content) for row in rows) else 0)
            + (30 if any(DISCUSSION_DEADLINE_TERMS.search(row.content) for row in rows) else 0)
            + min(len(rows), 20)
            + min(len(set(row.sender for row in rows)), 8) * 2
        )
        parsed_times = [parse_time_for_filter(row.time) for row in rows]
        parsed_times = [value for value in parsed_times if value]
        intermittent = any(
            (current - previous).total_seconds() > 45 * 60
            for previous, current in zip(parsed_times, parsed_times[1:])
        )
        start_label = format_message_time(rows[0].time)
        end_label = format_message_time(rows[-1].time)
        time_label = start_label if start_label == end_label else f"{start_label}-{end_label}"
        if intermittent:
            time_label += "（间歇）"
        threads.append(
            {
                "开始时间": rows[0].time,
                "结束时间": rows[-1].time,
                "时间段": time_label,
                "主题": topics,
                "消息数": len(rows),
                "发言人": speakers,
                "发言人数": speaker_count,
                "围绕什么": shorten(evidence, 190),
                "值得关注": attention,
                "关注等级": attention_level,
                "信号类型": categories,
                "有实质讨论": bool(DISCUSSION_SUBSTANCE_TERMS.search(" ".join(row.content for row in rows))),
                "具体信息": bool(
                    categories
                    or any(AMOUNT_PATTERN.search(row.content) for row in rows)
                    or any(DISCUSSION_DEADLINE_TERMS.search(row.content) for row in rows)
                    or any(DISCUSSION_INFORMATION_TERMS.search(row.content) for row in rows)
                ),
                "证据消息": compact_evidence_messages(representative_rows, limit=4),
                "仅链接投放": bool(
                    any(extract_urls(row.content) for row in rows)
                    and any(PAID_BOOST_TERMS.search(row.content) for row in rows)
                    and not categories
                ),
                "分数": score,
            }
        )
    return threads[:max_threads]


def load_discussion_threads(row: dict[str, str]) -> list[dict[str, Any]]:
    try:
        value = json.loads(row.get("讨论段落") or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def is_noise_evidence(content: str) -> bool:
    return (
        "SystemMessages_HongbaoIcon" in content
        or "领取了你的" in content
        or "领取了红包" in content
        or content.startswith("收到转账")
        or content.startswith("恭喜发财，大吉大利")
    )


LOW_VALUE_GROUP_MESSAGE_TERMS = re.compile(
    r"^(?:已?三连|已?点赞|已?转发|支持|收到|好的?|好滴|ok|okay|哈哈+|嘿嘿+|666+|冲+|赞+|蹲|围观|感谢分享|谢谢分享|辛苦了|[.。!！~～]+)$",
    re.I,
)


GROUP_VALUE_PATTERNS: dict[str, re.Pattern[str]] = {
    "AI": re.compile(r"(?:\bAI\b|人工智能|AIGC|Agent|智能体|大模型|LLM|GPT|Claude|Codex|DeepSeek|MCP)", re.I),
    "赚钱": re.compile(r"(?:赚钱|变现|收入|收益|付费|稿费|佣金|分成|预算|报价|结算|收款|奖励|有偿|回款)"),
    "培训": re.compile(r"(?:培训|内训|课程|讲师|授课|试讲|课时|学时|工作坊|分享会|教练|嘉宾)"),
    "商单": re.compile(r"(?:商单|品牌方|推广|投放|brief|博主招募|合作预算|内容合作|稿费|报价|Quote|Thread)", re.I),
    "出海": re.compile(r"(?:出海|海外|跨境|全球|国际市场|澳洲|悉尼|留学|海外市场|global)", re.I),
    "产品": re.compile(r"(?:产品|工具|SaaS|应用|插件|功能|上线|发布|内测|测评|API|开源|项目)", re.I),
    "Web3": re.compile(r"(?:Web3|链上|区块链|加密|币安|OKX|BTC|ETH|空投|钱包|DeFi)", re.I),
    "自媒体运营与增长": re.compile(
        r"(?:自媒体|内容运营|账号运营|内容增长|账号增长|涨粉|流量|曝光|完播|互动率|"
        r"选题|起号|矩阵号|多平台分发|小红书|抖音|视频号|公众号|B站|YouTube|X平台|Twitter)",
        re.I,
    ),
    "合作": re.compile(
        r"(?:项目合作|合作机会|合作方式|寻求合作|可以合作|联合共创|渠道合作|资源合作|"
        r"商务合作|品牌合作|培训合作|内容合作|转介绍|对接人|项目方|需求方)",
        re.I,
    ),
    "B端AI赋能": re.compile(
        r"(?:B端|企业\s*AI|AI\s*赋能|企业赋能|企业内训|企业培训|企业咨询|工作流体检|"
        r"业务提效|降本增效|组织提效|AI落地|数字化转型|FDE|私有化部署|知识库建设)",
        re.I,
    ),
}


def _compile_keyword_pattern(keywords: list[str]) -> re.Pattern[str] | None:
    values = [str(keyword).strip() for keyword in keywords if str(keyword).strip()]
    if not values:
        return None
    return re.compile("(?:" + "|".join(re.escape(value) for value in values) + ")", re.I)


def configured_group_value_patterns() -> dict[str, re.Pattern[str]]:
    patterns = dict(GROUP_VALUE_PATTERNS)
    priorities = ACTIVE_PROFILE.get("intelligence_priorities")
    if not isinstance(priorities, dict):
        return patterns
    custom_topics = priorities.get("custom_topics")
    if isinstance(custom_topics, dict):
        for raw_name, raw_keywords in custom_topics.items():
            name = str(raw_name).strip()
            if not name or not isinstance(raw_keywords, list):
                continue
            pattern = _compile_keyword_pattern(raw_keywords)
            if pattern:
                patterns[name] = pattern
    priority_pattern = _compile_keyword_pattern(priorities.get("priority_keywords") or [])
    if priority_pattern:
        patterns["个人重点"] = priority_pattern
    return patterns


def configured_focus_areas(patterns: dict[str, re.Pattern[str]]) -> list[str]:
    priorities = ACTIVE_PROFILE.get("intelligence_priorities")
    raw = priorities.get("focus_areas") if isinstance(priorities, dict) else []
    selected = [str(item).strip() for item in (raw or []) if str(item).strip() in patterns]
    custom = [name for name in patterns if name not in GROUP_VALUE_PATTERNS]
    return list(dict.fromkeys(selected + custom)) or list(patterns)


def configured_deprioritize_pattern() -> re.Pattern[str] | None:
    priorities = ACTIVE_PROFILE.get("intelligence_priorities")
    raw = priorities.get("deprioritize_keywords") if isinstance(priorities, dict) else []
    return _compile_keyword_pattern(raw or [])


def _context_document_paths(values: list[str] | None) -> list[Path]:
    selected: list[Path] = []
    for raw in values or []:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"找不到个人上下文文件或目录：{path}")
        if path.is_file():
            selected.append(path)
            continue
        candidates = sorted(
            candidate for candidate in path.rglob("*")
            if candidate.is_file()
            and candidate.suffix.lower() in {".md", ".txt"}
            and not any(part.startswith(".") for part in candidate.relative_to(path).parts)
        )
        selected.extend(candidates[:50])
    return list(dict.fromkeys(selected))


def _read_context_text(paths: list[Path]) -> str:
    parts: list[str] = []
    for path in paths:
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace")[:200_000])
        except OSError:
            continue
    return "\n".join(parts)


def infer_focus_areas_from_documents(paths: list[Path]) -> list[str]:
    text = _read_context_text(paths)
    if not text:
        return []
    scored = [
        (len(pattern.findall(text)), label)
        for label, pattern in GROUP_VALUE_PATTERNS.items()
    ]
    scored.sort(key=lambda item: (-item[0], list(GROUP_VALUE_PATTERNS).index(item[1])))
    return [label for count, label in scored if count > 0]


def _parse_custom_topics(values: list[str] | None) -> dict[str, list[str]]:
    topics: dict[str, list[str]] = {}
    for raw in values or []:
        name, separator, keywords = raw.partition("=")
        parsed = [item.strip() for item in re.split(r"[,，]", keywords) if item.strip()]
        if not separator or not name.strip() or not parsed:
            raise SystemExit("--custom-topic 格式应为 主题名=关键词1,关键词2")
        topics[name.strip()] = parsed
    return topics


def _suggest_label_roles(label_names: list[str]) -> dict[str, list[str]]:
    commercial = [
        label for label in label_names
        if re.search(r"商单|品牌|客户|商务|合作方|甲方|销售", label, re.I)
    ]
    creator = [
        label for label in label_names
        if re.search(r"自媒体|博主|创作者|同行|资源|媒体|达人|KOL", label, re.I)
    ]
    return {
        "commercial": commercial,
        "creator": creator,
        "priority": list(dict.fromkeys(commercial + creator)),
    }


def profile_init_command(args: argparse.Namespace) -> None:
    output_path = Path(args.out).expanduser().resolve()
    if output_path.exists() and not args.force:
        raise SystemExit(f"Profile 已存在：{output_path}。如需在保留原字段的基础上更新，请加 --force。")

    existing: dict[str, Any] = {}
    if output_path.exists():
        try:
            loaded = json.loads(output_path.read_text(encoding="utf-8"))
            existing = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    profile = merge_profile(DEFAULT_PROFILE, existing)

    existing_context = profile.get("context") if isinstance(profile.get("context"), dict) else {}
    personal_paths = (
        _context_document_paths(args.personal_doc)
        if args.personal_doc is not None
        else [Path(value).expanduser() for value in existing_context.get("personal_documents", [])]
    )
    plan_paths = (
        _context_document_paths(args.plan_doc)
        if args.plan_doc is not None
        else [Path(value).expanduser() for value in existing_context.get("current_plan_documents", [])]
    )
    all_context_paths = personal_paths + plan_paths
    inferred_focus = infer_focus_areas_from_documents(all_context_paths)
    explicit_focus = [item.strip() for item in (args.focus or []) if item.strip()]
    focus_areas = explicit_focus or inferred_focus or profile["intelligence_priorities"]["focus_areas"]

    if args.owner_alias:
        profile["owner_aliases"] = list(dict.fromkeys(item.strip() for item in args.owner_alias if item.strip()))
    if args.social_handle:
        profile["owner_social_handles"] = list(
            dict.fromkeys(item.strip().lstrip("@") for item in args.social_handle if item.strip())
        )

    labels = profile["labels"]
    for argument, key in (
        (args.priority_label, "priority"),
        (args.commercial_label, "commercial"),
        (args.creator_label, "creator"),
        (args.reactivation_label, "reactivation"),
    ):
        if argument:
            labels[key] = list(dict.fromkeys(item.strip() for item in argument if item.strip()))

    profile["context"] = {
        "setup_status": "ready" if all_context_paths and labels.get("priority") else "needs_context",
        "personal_documents": [str(path) for path in personal_paths],
        "current_plan_documents": [str(path) for path in plan_paths],
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    priorities = profile["intelligence_priorities"]
    priorities["focus_areas"] = list(dict.fromkeys(focus_areas))
    if args.priority_keyword:
        priorities["priority_keywords"] = list(
            dict.fromkeys(item.strip() for item in args.priority_keyword if item.strip())
        )
    if args.deprioritize_keyword:
        priorities["deprioritize_keywords"] = list(
            dict.fromkeys(item.strip() for item in args.deprioritize_keyword if item.strip())
        )
    custom_topics = _parse_custom_topics(args.custom_topic)
    if custom_topics:
        priorities["custom_topics"] = {**priorities.get("custom_topics", {}), **custom_topics}

    discovered_labels: list[str] = []
    if args.inspect_wechat_labels:
        wechat_cli = require_compatible_wechat_cli(args.wechat_cli)
        discovered_labels = sorted(fetch_wechat_labels(wechat_cli).values(), key=str.lower)
    suggestions = _suggest_label_roles(discovered_labels)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path = Path(args.report).expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    if not personal_paths:
        missing.append("个人说明：身份、正在经营的业务、擅长领域、资源、约束和长期目标")
    if not plan_paths:
        missing.append("当前计划：本月/本季度目标、正在推进的项目、收入或交付优先级、截止时间")
    if not labels.get("priority"):
        missing.append("重点微信标签：需要长期跟踪的客户、合作方、同行、资源方或其他角色")
    lines = [
        "# 微信个人情报库｜个性化初始化",
        "",
        f"- Profile：`{output_path}`",
        f"- 状态：`{profile['context']['setup_status']}`",
        f"- 识别重点：{'、'.join(priorities['focus_areas']) or '尚未设置'}",
        f"- 重点标签：{'、'.join(labels.get('priority') or []) or '尚未设置'}",
        "",
    ]
    if missing:
        lines.extend(["## 建议先准备", "", *(f"- {item}" for item in missing), ""])
    if discovered_labels:
        lines.extend(
            [
                "## 本机微信标签候选",
                "",
                "- 已有标签：" + "、".join(discovered_labels),
                "- 商业/客户候选：" + ("、".join(suggestions["commercial"]) or "未自动识别，请人工选择"),
                "- 同行/创作者候选：" + ("、".join(suggestions["creator"]) or "未自动识别，请人工选择"),
                "",
            ]
        )
    lines.extend(
        [
            "## 标签建议",
            "",
            "- 标签应围绕你需要持续跟踪的关系设计，不必照搬“商单推广 / 自媒体网友”。",
            "- 常见做法：客户、同行、渠道、供应商、自媒体网友、品牌方。",
            "- 同一联系人可以有多个标签；标签用于缩小优先扫描范围，不会限制全微信关键词搜索。",
            "",
            "## 下一步",
            "",
            "1. 补齐缺失材料或直接编辑 Profile。",
            "2. 运行 `profile-status` 检查是否可用。",
            "3. 运行 `wechat-labels` 导出所选标签联系人，再做首次 24 小时报告。",
        ]
    )
    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Profile：{output_path}")
    print(f"初始化报告：{report_path}")
    print(f"状态：{profile['context']['setup_status']}")


def profile_status_command(args: argparse.Namespace) -> None:
    context = ACTIVE_PROFILE.get("context") if isinstance(ACTIVE_PROFILE.get("context"), dict) else {}
    priorities = (
        ACTIVE_PROFILE.get("intelligence_priorities")
        if isinstance(ACTIVE_PROFILE.get("intelligence_priorities"), dict)
        else {}
    )
    labels = ACTIVE_PROFILE.get("labels") if isinstance(ACTIVE_PROFILE.get("labels"), dict) else {}
    status = str(context.get("setup_status") or "needs_context")
    issues: list[str] = []
    if ACTIVE_PROFILE_PATH is None:
        issues.append("尚未创建个人 Profile")
    if not context.get("personal_documents"):
        issues.append("未登记个人说明文档")
    if not context.get("current_plan_documents"):
        issues.append("未登记当前计划文档")
    if not labels.get("priority"):
        issues.append("未设置重点微信标签")
    if not priorities.get("focus_areas"):
        issues.append("未设置情报重点")
    payload = {
        "status": "ready" if not issues and status == "ready" else "needs_context",
        "profile": str(ACTIVE_PROFILE_PATH) if ACTIVE_PROFILE_PATH else None,
        "focus_areas": priorities.get("focus_areas") or [],
        "priority_labels": labels.get("priority") or [],
        "issues": issues,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"状态：{payload['status']}")
    print(f"Profile：{payload['profile'] or '未创建'}")
    print("情报重点：" + ("、".join(payload["focus_areas"]) or "未设置"))
    print("重点标签：" + ("、".join(payload["priority_labels"]) or "未设置"))
    for issue in issues:
        print(f"待补：{issue}")

COMMERCIAL_GROUP_NAME_PATTERN = re.compile(
    r"(?:商单|品牌|推广|宣发|助推|加热|Boost|博主|自媒体|运营|增长|客服群|快闪群)",
    re.I,
)
ENTERTAINMENT_GROUP_NAME_PATTERN = re.compile(
    r"(?:吃喝玩乐|体育|运动群|厨房|Lodge|天龙人|眉骨|匹克球|羽毛球|网球|"
    r"外卖|福利群|薅羊毛|演唱会粉丝群|校友闲聊|同学群)",
    re.I,
)
ENTERTAINMENT_CONTENT_PATTERN = re.compile(
    r"(?:足球|篮球|世界杯|球赛|比分|演唱会|追星|八卦|外卖|红包福利|拼单|薅羊毛|"
    r"吃饭|聚餐|喝酒|打牌|日常闲聊|旅游约伴|相亲|天气)",
    re.I,
)
ATTACHMENT_ONLY_PATTERN = re.compile(r"^\[(?:图片|表情|视频|文件|语音|位置|链接)\]$")


def is_low_value_group_message(content: str) -> bool:
    value = re.sub(r"\s+", "", content or "").strip()
    value = re.sub(r"^[#＃]接龙\s*", "", value, flags=re.I)
    value = re.sub(r"^\d+[.、)]", "", value)
    value = re.sub(r"[🙏👍👏💪🔥🎉🥳🤝✅☑️❤❤️]+", "", value).strip()
    return not value or bool(LOW_VALUE_GROUP_MESSAGE_TERMS.fullmatch(value))


def is_substantive_group_value_message(message: Message) -> bool:
    content = (message.content or "").strip()
    return bool(
        message.sender != "系统"
        and content
        and not ATTACHMENT_ONLY_PATTERN.fullmatch(content)
        and not is_noise_evidence(content)
        and not is_low_value_group_message(content)
        and not is_boost_coordination_message(content)
        and not is_group_recap_message(message)
        and not re.search(r"当前版本不支持展示|请升级至最新版本|撤回了一条消息", content)
    )


def group_activity_level(message_count: int) -> str:
    if message_count >= 300:
        return "极高"
    if message_count >= 100:
        return "高"
    if message_count >= 30:
        return "中"
    return "低"


def build_group_selection_rows(
    summaries: list[dict[str, str]],
    messages: list[Message],
) -> list[dict[str, Any]]:
    """Build an auditable, user-adjustable value matrix for every active group."""
    messages_by_group: dict[str, list[Message]] = {}
    for message in messages:
        messages_by_group.setdefault(message.chat, []).append(message)

    value_patterns = configured_group_value_patterns()
    focus_areas = configured_focus_areas(value_patterns)
    deprioritize_pattern = configured_deprioritize_pattern()
    rows: list[dict[str, Any]] = []
    for summary in summaries:
        group_name = str(summary.get("群聊") or "未命名群聊")
        group_messages = messages_by_group.get(group_name, [])
        substantive = [message for message in group_messages if is_substantive_group_value_message(message)]
        substantive_text = "\n".join(message.content for message in substantive)
        identity_text = " ".join(
            [group_name, str(summary.get("主要主题") or ""), substantive_text]
        )
        current_counts = {
            label: sum(1 for message in substantive if pattern.search(message.content or ""))
            for label, pattern in value_patterns.items()
        }
        dimensions = {
            label: bool(pattern.search(identity_text))
            for label, pattern in value_patterns.items()
        }
        paid_boost_count = sum(
            1
            for message in group_messages
            if is_boost_coordination_message(message.content or "") and extract_urls(message.content or "")
        )
        commercial_identity = bool(COMMERCIAL_GROUP_NAME_PATTERN.search(group_name))
        direct_business_count = sum(
            current_counts[label]
            for label in ("赚钱", "培训", "商单", "合作", "B端AI赋能")
        )
        strategic_labels = [
            label for label in focus_areas
            if label not in {"赚钱", "培训", "商单", "合作", "B端AI赋能", "个人重点"}
        ]
        strategic_count = sum(bool(dimensions.get(label)) for label in strategic_labels)
        personal_priority_count = current_counts.get("个人重点", 0) + sum(
            current_counts.get(label, 0)
            for label in focus_areas
            if label not in GROUP_VALUE_PATTERNS and label != "个人重点"
        )
        deprioritized = bool(deprioritize_pattern and deprioritize_pattern.search(identity_text))
        entertainment_identity = bool(ENTERTAINMENT_GROUP_NAME_PATTERN.search(group_name))
        entertainment_message_count = sum(
            1 for message in substantive if ENTERTAINMENT_CONTENT_PATTERN.search(message.content or "")
        )
        entertainment_dominant = bool(
            substantive
            and entertainment_message_count / len(substantive) >= 0.45
            and not direct_business_count
            and not strategic_count
        )
        system_only = not substantive

        if direct_business_count or personal_priority_count:
            suggested_level = "重点"
            rationale = (
                "本时段命中个人重点主题或关键词"
                if personal_priority_count and not direct_business_count
                else "本时段出现可推进的商单、培训或收入信号"
            )
        elif commercial_identity or paid_boost_count:
            suggested_level = "雷达观察"
            rationale = "属于商业资源群或出现付费加热，只看新增项目和跨群链接即可"
        elif entertainment_identity or entertainment_dominant or deprioritized:
            suggested_level = "低优先级"
            rationale = (
                "命中个人降噪关键词，且没有更高优先级行动信号"
                if deprioritized and not (entertainment_identity or entertainment_dominant)
                else "本时段以运动、生活或娱乐闲聊为主，没有可执行商业信号"
            )
        elif strategic_count >= 2 and substantive:
            suggested_level = "关注"
            rationale = "讨论与 AI、出海、产品、Web3 或自媒体增长等当前方向相关"
        elif system_only:
            suggested_level = "低优先级"
            rationale = "本时段只有系统通知或附件占位，没有可读讨论"
        elif not any(dimensions.values()):
            suggested_level = "低优先级"
            rationale = "本时段未命中当前十类目标，主要是一般闲聊或信息同步"
        else:
            suggested_level = "关注"
            rationale = "有单一方向相关信息，暂未形成明确行动"

        message_count = int(summary.get("消息数") or len(group_messages) or 0)
        if entertainment_identity or entertainment_dominant:
            group_type = "低价值娱乐/闲聊"
        elif direct_business_count or personal_priority_count:
            group_type = "商业与合作"
        elif commercial_identity or paid_boost_count:
            group_type = "商单雷达"
        elif strategic_count:
            group_type = "目标方向相关"
        else:
            group_type = "一般信息"

        value_counts = {f"{label}消息数": current_counts[label] for label in value_patterns}
        rows.append(
            {
                "群聊": group_name,
                "消息数": message_count,
                "有效讨论数": len(substantive),
                "发言人数": int(summary.get("发言人数") or 0),
                "活跃度": group_activity_level(message_count),
                "AI": dimensions["AI"],
                "赚钱": dimensions["赚钱"],
                "培训": dimensions["培训"],
                "商单": dimensions["商单"],
                "出海": dimensions["出海"],
                "产品": dimensions["产品"],
                "Web3": dimensions["Web3"],
                "自媒体运营与增长": dimensions["自媒体运营与增长"],
                "合作": dimensions["合作"],
                "B端AI赋能": dimensions["B端AI赋能"],
                **{
                    label: dimensions[label]
                    for label in value_patterns
                    if label not in GROUP_VALUE_PATTERNS
                },
                **value_counts,
                "付费加热链接数": paid_boost_count,
                "群聊类型": group_type,
                "建议关注级别": suggested_level,
                "判断依据": rationale,
                "主要主题": str(summary.get("主要主题") or "未识别"),
            }
        )

    level_rank = {"重点": 4, "雷达观察": 3, "关注": 2, "低优先级": 1}
    activity_rank = {"极高": 4, "高": 3, "中": 2, "低": 1}
    rows.sort(
        key=lambda row: (
            level_rank.get(str(row["建议关注级别"]), 0),
            activity_rank.get(str(row["活跃度"]), 0),
            int(row["消息数"]),
        ),
        reverse=True,
    )
    return rows


def write_group_selection_matrix(
    out_dir: Path,
    summaries: list[dict[str, str]],
    messages: list[Message],
    since: str,
    until: str,
) -> list[dict[str, Any]]:
    rows = build_group_selection_rows(summaries, messages)
    dimensions = list(configured_group_value_patterns())
    payload = {
        "since": since,
        "until": until,
        "basis": (
            "按本机个人 Profile 校准，当前重点："
            + "、".join(configured_focus_areas(configured_group_value_patterns()))
            + "。建议级别只针对本时段，不等于永久排除。"
        ),
        "dimensions": dimensions,
        "groups": rows,
    }
    (out_dir / "group_selection_matrix.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    dynamic_dimensions = [label for label in dimensions if label not in GROUP_VALUE_PATTERNS]
    fieldnames = [
        "群聊", "消息数", "有效讨论数", "发言人数", "活跃度", "AI", "赚钱", "培训",
        "商单", "出海", "产品", "Web3", "自媒体运营与增长", "合作", "B端AI赋能",
        "AI消息数", "赚钱消息数", "培训消息数", "商单消息数", "出海消息数", "产品消息数",
        "Web3消息数", "自媒体运营与增长消息数", "合作消息数", "B端AI赋能消息数",
        *dynamic_dimensions,
        *(f"{label}消息数" for label in dynamic_dimensions),
        "付费加热链接数", "群聊类型", "建议关注级别", "判断依据", "主要主题",
    ]
    write_csv(out_dir / "group_selection_matrix.csv", rows, fieldnames)
    lines = [
        "# 全部群聊价值矩阵",
        "",
        f"> 时间范围：{since} 至 {until}。建议级别只描述本时段，不会自动写入永久排除名单。",
        "",
        "| 群聊 | 类型 | 活跃度 | 消息/有效讨论 | 相关方向 | 建议级别 | 判断依据 |",
        "|---|---|---:|---:|---|---|---|",
    ]
    for row in rows:
        tags = " / ".join(label for label in dimensions if row.get(label)) or "无"
        values = [
            str(row["群聊"]), str(row["群聊类型"]), str(row["活跃度"]),
            f"{row['消息数']}/{row['有效讨论数']}", tags,
            str(row["建议关注级别"]), str(row["判断依据"]),
        ]
        values = [value.replace("|", "\\|").replace("\n", " ") for value in values]
        lines.append("| " + " | ".join(values) + " |")
    (out_dir / "group_selection_matrix.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows


def evidence_dedupe_key(content: str) -> str:
    urls = extract_urls(content)
    if urls:
        return "url:" + "|".join(sorted(urls))
    cleaned = re.sub(r"https?://\S+", "", content)
    cleaned = re.sub(r"#?接龙", "", cleaned)
    cleaned = re.sub(r"\b\d+[\.、)]", "", cleaned)
    cleaned = re.sub(r"[①②③④⑤⑥⑦⑧⑨⑩1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣8️⃣9️⃣🔟]", "", cleaned)
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = re.sub(r"[，。！？、；：,.!?:;|｜\-—_#*（）()\[\]【】\"'“”‘’]", "", cleaned)
    return "text:" + cleaned[:120]


def compact_key_messages(messages: list[Message], limit: int = 3) -> list[str]:
    scored: list[tuple[int, Message]] = []
    for message in messages:
        content = message.content.strip()
        if (
            not content
            or content.startswith("[") and content.endswith("]")
            or is_noise_evidence(content)
            or is_low_value_group_message(content)
            or is_boost_coordination_message(content)
            or is_group_recap_message(message)
        ):
            continue
        score = min(len(content), 120)
        if MONETIZATION_TERMS.search(content):
            score += 80
        if DIRECT_DEAL_TERMS.search(content):
            score += 60
        if AMOUNT_PATTERN.search(content):
            score += 50
        scored.append((score, message))
    scored.sort(key=lambda item: (item[0], item[1].time), reverse=True)
    snippets: list[str] = []
    seen_keys: set[str] = set()
    for _, message in scored:
        key = evidence_dedupe_key(message.content)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        snippet = f"{message.sender}：{shorten(message.content, 96)}"
        if snippet not in snippets:
            snippets.append(snippet)
        if len(snippets) >= limit:
            break
    return snippets


def summarize_group(messages: list[Message], joined_channel_markers: list[str] | None = None) -> dict[str, str]:
    if not messages:
        return {}
    recap_entries = group_recap_entries(messages)
    primary_messages = [message for message in messages if not is_group_recap_message(message)]
    speakers = Counter(message.sender for message in messages if message.sender)
    topics = group_topics(primary_messages)
    signals = extract_signals(primary_messages)
    joined_channel_markers = joined_channel_markers or []
    project_collaboration = is_project_collaboration_group(messages[0].chat, primary_messages or messages)
    discussion_threads = build_group_discussion_threads(
        primary_messages,
        project_collaboration=project_collaboration,
    )
    discussion_signal_counts: Counter[str] = Counter(
        category
        for thread in discussion_threads
        for category in thread.get("信号类型", [])
    )
    important_discussion_count = sum(
        1
        for thread in discussion_threads
        if thread.get("关注等级") in {"立即处理", "值得关注"}
    )
    immediate_discussion_count = sum(
        1 for thread in discussion_threads if thread.get("关注等级") == "立即处理"
    )

    def eligible(message: Message) -> bool:
        return not (
            message.sender == "系统"
            or is_noise_evidence(message.content)
            or is_boost_coordination_message(message.content)
            or EXPLICIT_NON_DEAL_TERMS.search(message.content)
            or is_group_recap_message(message)
            or is_joined_channel_repeat_invite(message, joined_channel_markers)
        )

    if project_collaboration:
        deal_messages = [message for message in primary_messages if eligible(message) and PIPELINE_CONTEXT_TERMS.search(message.content)]
    else:
        deal_messages = [
            message
            for message in primary_messages
            if eligible(message) and GROUP_DIGEST_ACTIONABLE_DEAL_TERMS.search(message.content)
        ]
    training_project_messages = [
        message
        for message in primary_messages
        if eligible(message)
        and is_training_project_opportunity(message)
        and GROUP_DIGEST_ACTIONABLE_TRAINING_TERMS.search(message.content)
    ]
    opportunity_messages = dedupe_messages(deal_messages + training_project_messages)
    unique_opportunity_count = len({evidence_dedupe_key(message.content) for message in opportunity_messages if not is_noise_evidence(message.content)})
    unique_deal_count = len({evidence_dedupe_key(message.content) for message in deal_messages if not is_noise_evidence(message.content)})
    unique_training_project_count = len(
        {evidence_dedupe_key(message.content) for message in training_project_messages if not is_noise_evidence(message.content)}
    )
    opportunity_signals = extract_signals(opportunity_messages)
    topic_text = "、".join(topics) if topics else "日常闲聊/信息同步"
    active_speakers = "、".join(name for name, _ in speakers.most_common(5))
    key_messages = " | ".join(compact_key_messages(primary_messages))
    timeline_summary = build_group_timeline(primary_messages)
    opportunity_summary = " | ".join(compact_key_messages(opportunity_messages, limit=4))
    deal_summary = " | ".join(compact_key_messages(deal_messages, limit=4))
    training_project_summary = " | ".join(compact_key_messages(training_project_messages, limit=4))
    attachment_note = adjacent_attachment_note(primary_messages, deal_messages + training_project_messages)
    latest = max(messages, key=lambda message: message.time or "")
    strongest_stage = max(signals, key=lambda signal: stage_rank(signal.stage)).stage if signals and opportunity_messages else ""
    if project_collaboration and unique_deal_count > 0:
        action = "按对方最新要求推进交付、审核、发布、数据回传或结算，并核对截止时间"
    elif unique_training_project_count > 0:
        action = "培训/咨询/项目合作线索：优先展开前后对话和附件，确认需求方、预算、课时、档期与转介绍路径"
    elif unique_deal_count > 0:
        action = infer_group_action(topics, opportunity_messages, unique_opportunity_count)
    elif discussion_signal_counts["项目合作"]:
        action = "项目合作候选：确认发起人、交付范围、预算、时间和是否还需要人选"
    elif discussion_signal_counts["活动"]:
        action = "活动信号：确认主办方、时间地点、参与方式和是否存在嘉宾/讲师/合作名额"
    elif discussion_signal_counts["招聘/外包"]:
        action = "招聘/外包信号：先看完整要求，再判断是否适合本人、可转介绍或可延伸为项目合作"
    elif discussion_signal_counts["赚钱/奖励"]:
        action = "赚钱/奖励候选：核实参与门槛、收益规则、结算方式和时间成本"
    else:
        action = infer_group_action(topics, opportunity_messages, unique_opportunity_count)
    return {
        "群聊": messages[0].chat,
        "消息数": str(len(messages)),
        "有效消息数": str(len(primary_messages)),
        "发言人数": str(len(speakers)),
        "活跃发言人": active_speakers,
        "最后消息时间": latest.time,
        "主要主题": topic_text,
        "商单信号数": str(len(signals)),
        "变现机会数": str(unique_opportunity_count),
        "商单机会数": str(unique_deal_count),
        "培训/项目合作机会数": str(unique_training_project_count),
        "重要讨论数": str(important_discussion_count),
        "立即处理讨论数": str(immediate_discussion_count),
        "重要信号数": str(sum(discussion_signal_counts.values())),
        "项目合作信号数": str(discussion_signal_counts["项目合作"]),
        "活动信号数": str(discussion_signal_counts["活动"]),
        "招聘/外包信号数": str(discussion_signal_counts["招聘/外包"]),
        "赚钱/奖励信号数": str(discussion_signal_counts["赚钱/奖励"]),
        "当前阶段": strongest_stage,
        "建议动作": action,
        "讨论段落": json.dumps(discussion_threads, ensure_ascii=False),
        "群聊脉络": timeline_summary,
        "关键发言": key_messages,
        "商单/变现机会摘要": opportunity_summary,
        "商单机会摘要": deal_summary,
        "培训/项目合作摘要": training_project_summary,
        "附件核验提示": attachment_note,
        "已有日报数": str(len(recap_entries)),
        "群内已有日报": json.dumps(recap_entries, ensure_ascii=False),
        "机会信号数": str(len(opportunity_signals)),
        "跨群高概率商单": "0",
        "跨群疑似商单": "0",
    }


def _digest_int(row: dict[str, str], key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _digest_snippet(value: str, limit: int = 120, max_items: int = 2) -> str:
    parts = [part.strip() for part in re.split(r"\s*\|\s*", value or "") if part.strip()]
    value = "；".join(parts[:max_items])
    value = re.sub(r"https?://\S+", "[链接见下方聚合]", value)
    value = re.sub(r"\s+", " ", value).strip(" ；")
    return shorten(value, limit) if value else "需要打开原群核实上下文。"


def _group_digest_tags(row: dict[str, str]) -> list[str]:
    tags: list[str] = []
    if _digest_int(row, "商单机会数"):
        tags.append("商单")
    if _digest_int(row, "培训/项目合作机会数"):
        tags.append("培训/项目")
    for key, label in (
        ("项目合作信号数", "项目合作"),
        ("活动信号数", "活动"),
        ("招聘/外包信号数", "招聘/外包"),
        ("赚钱/奖励信号数", "赚钱/奖励"),
    ):
        if _digest_int(row, key) and label not in tags:
            tags.append(label)
    if row.get("附件核验提示"):
        tags.append("附件待核实")
    return tags


def select_group_action_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    selected = [
        row
        for row in rows
        if _digest_int(row, "商单机会数")
        or _digest_int(row, "培训/项目合作机会数")
        or _digest_int(row, "项目合作信号数")
        or _digest_int(row, "立即处理讨论数")
    ]
    selected.sort(
        key=lambda row: (
            _digest_int(row, "跨群高概率商单"),
            _digest_int(row, "培训/项目合作机会数"),
            _digest_int(row, "商单机会数"),
            _digest_int(row, "重要信号数"),
            row.get("最后消息时间", ""),
        ),
        reverse=True,
    )
    return selected


def build_group_editorial_packet(
    rows: list[dict[str, str]],
    since: str,
    until: str,
    link_rows: list[dict[str, str]],
    *,
    max_discussions: int = 36,
) -> dict[str, Any]:
    action_rows = select_group_action_rows(rows)
    action_items: list[dict[str, Any]] = []
    action_ids = {id(row) for row in action_rows}
    discussion_candidates: list[dict[str, Any]] = []
    recap_index: list[dict[str, Any]] = []

    for row in rows:
        threads = load_discussion_threads(row)
        recaps = load_group_recaps(row)
        if recaps:
            recap_index.append({"群聊": row.get("群聊", ""), "日报": recaps})
        signal_threads = [thread for thread in threads if thread.get("信号类型")]
        if id(row) in action_ids:
            group_name = row.get("群聊", "")
            related_links = [
                {
                    "链接": link.get("链接", ""),
                    "商单判断": link.get("商单判断", ""),
                    "判断依据": link.get("判断依据", ""),
                    "覆盖群数": link.get("覆盖群数", ""),
                    "出现次数": link.get("出现次数", ""),
                    "相关群聊": link.get("相关群聊", ""),
                    "证据摘要": link.get("证据摘要", ""),
                }
                for link in link_rows
                if group_name and group_name in str(link.get("相关群聊") or "").split("、")
                and link.get("商单判断") in {"高概率商单", "疑似商单"}
            ]
            action_items.append(
                {
                    "群聊": group_name,
                    "标签": _group_digest_tags(row),
                    "最后消息时间": row.get("最后消息时间", ""),
                    "摘要候选": {
                        "商单": row.get("商单机会摘要", ""),
                        "培训或项目": row.get("培训/项目合作摘要", ""),
                        "关键发言": row.get("关键发言", ""),
                    },
                    "建议动作候选": row.get("建议动作", ""),
                    "附件核验提示": row.get("附件核验提示", ""),
                    "群内已有日报": recaps[:5],
                    "信号讨论": signal_threads[:4],
                    "相关跨群链接": related_links[:8],
                }
            )
        for thread in threads:
            if thread.get("仅链接投放") or thread.get("主题") == ["日常闲聊/信息同步"]:
                continue
            if not (
                thread.get("信号类型")
                or thread.get("具体信息")
                or thread.get("有实质讨论")
            ):
                continue
            discussion_candidates.append(
                {
                    "群聊": row.get("群聊", ""),
                    "最后消息时间": row.get("最后消息时间", ""),
                    **thread,
                }
            )

    discussion_candidates.sort(
        key=lambda thread: (
            bool(thread.get("信号类型")),
            bool(thread.get("具体信息")),
            int(thread.get("分数") or 0),
            thread.get("结束时间", ""),
        ),
        reverse=True,
    )
    high_links = [row for row in link_rows if row.get("商单判断") == "高概率商单"]
    suspected_links = [row for row in link_rows if row.get("商单判断") == "疑似商单"]
    return {
        "schema_version": 3,
        "用途": "供 Codex/Claude 做第二遍语义编辑；不是可直接交付给用户的日报",
        "时间范围": {"开始": since, "结束": until},
        "覆盖": {
            "活跃群聊": len(rows),
            "行动候选群聊": len(action_items),
            "讨论候选": min(len(discussion_candidates), max_discussions),
            "高概率跨群链接": len(high_links),
            "待核实跨群链接": len(suspected_links),
        },
        "编辑要求": [
            "同时生成话题日报 group_daily_topics.md 和重点群聊 group_daily_groups.md；group_daily_brief.md 只作兼容入口",
            "话题日报按真实项目、事件、问题或争议跨群归纳，保留旧版编辑式结构",
            "重点群聊只收录与当前用户目标直接相关且有信息增量的群，不列普通活跃群和无关娱乐群",
            "同一群内按讨论段合并原话；不要把证据摘录直接冒充群聊总结",
            "跨群共同主题只能作为补充观察，不得取代按群结构或重复复述各群内容",
            "机会必须说明与当前用户的关系、可联系的人、下一步和证据等级",
            "普通活跃讨论只在确有信息增量时进入正文",
            "同一链接或同一合作轮次只在跨群链接索引出现一次，群聊正文不重复 URL",
            "所有事实结论保留群聊、时间和发言人来源",
            "群内已有日报只作为二手参考和核验入口，不得重新作为关键发言、主题证据或商机证据",
            "红包、接龙、三连、四连和纯加热话术只进入跨群商单雷达，不进入话题日报或重点群聊正文",
            "不单独交付证据附录；在对应话题或群聊条目下直接保留群名、时间、发言人和必要原链接",
        ],
        "行动候选": action_items[:15],
        "讨论候选": discussion_candidates[:max_discussions],
        "群内已有日报索引": recap_index,
        "高概率跨群链接": high_links[:12],
        "待核实链接": suspected_links[:12],
    }


def write_group_editorial_packet(
    path: Path,
    rows: list[dict[str, str]],
    since: str,
    until: str,
    link_rows: list[dict[str, str]],
) -> None:
    packet = build_group_editorial_packet(rows, since, until, link_rows)
    path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")


def write_group_daily_appendix(path: Path, rows: list[dict[str, str]], since: str, until: str) -> None:
    lines = [
        "# 微信群聊日报证据附录",
        "",
        f"- 时间范围：{since} 至 {until}",
        f"- 群聊：{len(rows)} 个",
        "- 说明：本文件保留完整群级脉络，主日报只呈现需要行动或值得知道的内容。",
        "",
    ]
    for row in rows:
        threads = load_discussion_threads(row)
        recaps = load_group_recaps(row)
        lines.extend(
            [
                f"## {row['群聊']}",
                "",
                f"- 消息：{row['消息数']} 条；有效消息：{row.get('有效消息数', row['消息数'])} 条；"
                f"发言人：{row['发言人数']} 人；最后消息：{row['最后消息时间']}",
                f"- 主题：{row['主要主题']}",
                f"- 活跃发言人：{row['活跃发言人'] or '未识别'}",
                f"- 关键发言：{row['关键发言'] or '无明显关键发言。'}",
                "",
            ]
        )
        if recaps:
            lines.extend(["### 群内已有日报", ""])
            for recap in recaps:
                lines.append(
                    f"- {recap.get('时间', '')}｜{recap.get('发言人', '')}｜{recap.get('形式', '')}："
                    f"{recap.get('摘要', '')}"
                )
            lines.extend(["", "> 上述日报已从主题、关键发言和商机证据中排除，仅供交叉核验。", ""])
        if threads:
            lines.extend(["### 群内大事与讨论段落", ""])
            for thread in threads:
                topic_label = " / ".join(thread.get("主题") or ["其他"])
                signal_label = " / ".join(thread.get("信号类型") or [])
                lines.append(
                    f"- **{thread.get('时间段') or '时间未识别'}｜{topic_label}**："
                    f"{thread.get('围绕什么') or '无可用摘要'}"
                )
                lines.append(f"  - 关注：{thread.get('值得关注') or '背景动态'}")
                if signal_label:
                    lines.append(f"  - 信号：{signal_label}")
            lines.append("")
        else:
            lines.extend([f"- 群聊脉络：{row['群聊脉络'] or '无可用脉络。'}", ""])
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def group_digest_summary(row: dict[str, str]) -> str:
    topics = [item.strip() for item in row.get("主要主题", "").split("、") if item.strip()]
    parts = [f"主要围绕：{'、'.join(topics[:3])}" if topics else "以日常信息同步为主"]
    reasons = group_digest_reasons(row)
    if reasons:
        parts.append(f"检出：{'、'.join(reasons[:3])}")
    elif _digest_int(row, "重要讨论数"):
        parts.append(f"有 {_digest_int(row, '重要讨论数')} 段值得关注的讨论")
    else:
        parts.append("暂未发现需要立即处理的明确事项")
    return "；".join(parts) + "。"


def group_digest_reasons(row: dict[str, str]) -> list[str]:
    reasons: list[str] = []
    for key, label in (
        ("商单机会数", "明确商单"),
        ("培训/项目合作机会数", "培训/项目需求"),
        ("项目合作信号数", "项目合作"),
        ("活动信号数", "活动"),
        ("招聘/外包信号数", "招聘/外包"),
        ("赚钱/奖励信号数", "赚钱/奖励"),
    ):
        count = _digest_int(row, key)
        if count:
            reasons.append(f"{count} 条{label}")
    return unique_items(reasons)


def group_digest_sort_key(row: dict[str, str]) -> tuple[Any, ...]:
    return (
        bool(group_digest_reasons(row)),
        _digest_int(row, "重要信号数"),
        _digest_int(row, "重要讨论数"),
        row.get("最后消息时间", ""),
    )


def write_group_daily_digest(
    path: Path,
    rows: list[dict[str, str]],
    since: str,
    until: str,
    link_rows: list[dict[str, str]] | None = None,
    coverage: dict[str, Any] | None = None,
) -> None:
    link_rows = link_rows or []
    action_rows = select_group_action_rows(rows)
    action_names = {row.get("群聊", "") for row in action_rows}
    ordered_rows = [
        row
        for row in sorted(rows, key=group_digest_sort_key, reverse=True)
        if row.get("群聊", "") in action_names or _digest_int(row, "重要讨论数")
    ]
    focus_rows = [
        row
        for row in ordered_rows
        if row.get("群聊", "") in action_names or _digest_int(row, "重要讨论数")
    ]
    high_links = [row for row in link_rows if row.get("商单判断") == "高概率商单"]
    suspected_links = [row for row in link_rows if row.get("商单判断") == "疑似商单"]
    noteworthy_groups = sum(1 for row in rows if _digest_int(row, "重要讨论数"))
    recap_count = sum(_digest_int(row, "已有日报数") for row in rows)
    appendix_path = path.with_name("group_daily_appendix.md")

    lines = [
        "# 微信群聊日报",
        "",
        f"> {since} 至 {until}｜覆盖 {len(rows)} 个活跃群聊",
        "",
        "## 一眼结论",
        "",
        f"- 需要处理或核实：**{len(action_rows)}** 个群",
        f"- 有重点讨论：**{noteworthy_groups}** 个群",
        f"- 群内已有日报：**{recap_count}** 份，已从本报告主题与证据中排除",
        f"- 跨群投放信号：高概率 **{len(high_links)}** 个，待核实 **{len(suspected_links)}** 个",
        "- 阅读方式：先看行动清单，再看真正有信息增量的重点群聊；普通活跃群与纯加热接龙不进入正文。",
        "",
        "## 先处理这些",
        "",
    ]
    if coverage and coverage.get("failed"):
        lines[3:3] = [
            f"> ⚠️ 读取成功 {coverage.get('succeeded', 0)}/{coverage.get('requested', 0)} 个群；"
            f"{coverage.get('failed', 0)} 个群读取失败，不能据此判断这些群没有新消息。",
            "",
        ]
    if coverage and coverage.get("incomplete"):
        lines[3:3] = [f"> 覆盖不完整：{coverage['incomplete']} 个群仍有未读取的分页；当前结论仅基于已读取消息。", ""]
    if not action_rows:
        lines.append("当前窗口没有达到行动门槛的群聊线索。")
    for row in action_rows[:10]:
        tags = " / ".join(_group_digest_tags(row)) or "待核实"
        reasons = "；".join(group_digest_reasons(row)) or "存在需要核实的商业上下文"
        lines.append(
            f"- **{row['群聊']}｜{tags}**：{group_digest_summary(row)} "
            f"原因：{reasons}。下一步：{row['建议动作']}"
        )
    if len(action_rows) > 10:
        lines.append(f"- 另有 {len(action_rows) - 10} 个行动候选，可在下方按群展开。")

    lines.extend(
        [
            "",
            "## 按群查看",
            "",
            "> 只收录与当前目标相关且有信息增量的群；每条直接保留群名、时间、发言人和必要链接。",
            "",
        ]
    )
    for row in focus_rows:
        tags = " / ".join(_group_digest_tags(row)) or "普通讨论"
        reasons = group_digest_reasons(row)
        threads = [
            thread
            for thread in load_discussion_threads(row)
            if thread.get("关注等级") in {"立即处理", "值得关注"}
        ]
        recaps = load_group_recaps(row)
        lines.extend(
            [
                f"### {row['群聊']}",
                "",
                f"> {tags}｜{row['消息数']} 条，有效 {row.get('有效消息数', row['消息数'])} 条 / {row['发言人数']} 人",
                "",
                f"- **群聊总结**：{group_digest_summary(row)}",
                f"- **主要主题**：{row['主要主题'] or '未识别'}",
                f"- **时间**：最后消息 {row['最后消息时间']}",
            ]
        )
        if reasons:
            lines.append(f"- **值得关注**：{'；'.join(reasons)}")
        if row.get("群聊") in action_names:
            lines.append(f"- **建议动作**：{row['建议动作']}")
        if row.get("附件核验提示"):
            lines.append(f"- **附件**：{row['附件核验提示']}")
        if recaps:
            lines.extend(["", "### 群内已有日报", ""])
            for recap in recaps[:5]:
                lines.append(
                    f"- {recap.get('时间', '')}｜{recap.get('发言人', '')}｜{recap.get('形式', '')}："
                    f"{recap.get('摘要', '')}"
                )
            lines.append("- 这些内容只作为二手参考，不参与本日报的主题、关键发言或商机判断。")
        if threads:
            lines.extend(["", "### 群内大事", ""])
            for thread in threads[:4]:
                topic_label = " / ".join(thread.get("主题") or ["其他"])
                signal_label = " / ".join(thread.get("信号类型") or [])
                lines.append(
                    f"- **{thread.get('时间段') or '时间未识别'}｜{topic_label}**："
                    f"{_digest_snippet(thread.get('围绕什么', ''), 180)}"
                )
                lines.append(f"  - 关注：{thread.get('值得关注') or '背景动态'}")
                if signal_label:
                    lines.append(f"  - 信号：{signal_label}")
                evidence_messages = thread.get("证据消息") or []
                for evidence in evidence_messages[:3]:
                    lines.append(
                        f"  - 证据：{evidence.get('时间', '')}｜{evidence.get('发言人', '')}："
                        f"{shorten(str(evidence.get('内容') or ''), 150)}"
                    )
        lines.append("")

    lines.extend(
        [
            "## 商单雷达链接",
            "",
            f"- 保留：高概率 {len(high_links)} 个，待核实 {len(suspected_links)} 个。",
            "- 纯红包接龙不进入群聊总结；同一 URL 在这里仅出现一次。",
        ]
    )
    for link in (high_links + suspected_links)[:15]:
        lines.append(format_link_row_for_digest(link))
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    write_group_daily_appendix(appendix_path, rows, since, until)


def write_group_daily_html(
    path: Path,
    rows: list[dict[str, str]],
    since: str,
    until: str,
    link_rows: list[dict[str, str]] | None = None,
    coverage: dict[str, Any] | None = None,
) -> None:
    link_rows = link_rows or []
    action_names = {row.get("群聊", "") for row in select_group_action_rows(rows)}
    ordered_rows = [
        row
        for row in sorted(rows, key=group_digest_sort_key, reverse=True)
        if row.get("群聊", "") in action_names or _digest_int(row, "重要讨论数")
    ]
    high_links = [row for row in link_rows if row.get("商单判断") == "高概率商单"]
    suspected_links = [row for row in link_rows if row.get("商单判断") == "疑似商单"]
    recap_count = sum(_digest_int(row, "已有日报数") for row in rows)
    group_blocks: list[str] = []
    coverage_warning = ""
    if coverage and coverage.get("failed"):
        coverage_warning = (
            f'<p class="coverage-warning">读取成功 {coverage.get("succeeded", 0)}/'
            f'{coverage.get("requested", 0)} 个群；{coverage.get("failed", 0)} 个群读取失败。'
            "这些群不能判定为没有新消息。</p>"
        )
    for row in ordered_rows:
        is_action = row.get("群聊", "") in action_names
        row_kind = "action" if is_action else "important"
        tags = _group_digest_tags(row) or ["普通讨论"]
        threads = [
            thread
            for thread in load_discussion_threads(row)
            if thread.get("关注等级") in {"立即处理", "值得关注"}
        ]
        recaps = load_group_recaps(row)
        thread_blocks: list[str] = []
        for thread in threads[:4]:
            topic_label = " / ".join(thread.get("主题") or ["其他"])
            signals = " / ".join(thread.get("信号类型") or [])
            evidence_items = "".join(
                f"<li><time>{html_escape(str(item.get('时间') or ''))}</time> "
                f"<strong>{html_escape(str(item.get('发言人') or ''))}</strong>："
                f"{html_escape(shorten(str(item.get('内容') or ''), 180))}</li>"
                for item in (thread.get("证据消息") or [])[:3]
            )
            evidence_block = f'<ul class="evidence">{evidence_items}</ul>' if evidence_items else ""
            thread_blocks.append(
                "<section class=\"thread\">"
                f"<h3>{html_escape(str(thread.get('时间段') or '时间未识别'))}｜{html_escape(topic_label)}</h3>"
                f"<p>{html_escape(_digest_snippet(str(thread.get('围绕什么') or ''), 220))}</p>"
                f"<p class=\"muted\">关注：{html_escape(str(thread.get('值得关注') or '背景动态'))}"
                f"{('；信号：' + html_escape(signals)) if signals else ''}</p>"
                f"{evidence_block}"
                "</section>"
            )
        reason_text = "；".join(group_digest_reasons(row))
        search_text = " ".join(
            [row.get("群聊", ""), row.get("主要主题", ""), group_digest_summary(row), " ".join(tags), reason_text]
        ).casefold()
        badge_html = "".join(f"<span>{html_escape(tag)}</span>" for tag in tags)
        topic_badges = "".join(
            f"<span>{html_escape(topic.strip())}</span>"
            for topic in str(row.get("主要主题") or "").split("、")[:5]
            if topic.strip()
        )
        recap_items = "".join(
            f"<li><time>{html_escape(str(item.get('时间') or ''))}</time> "
            f"<strong>{html_escape(str(item.get('发言人') or ''))}</strong> · "
            f"{html_escape(str(item.get('形式') or ''))}：{html_escape(str(item.get('摘要') or ''))}</li>"
            for item in recaps[:5]
        )
        recap_html = (
            '<section class="recaps"><h3>群内已有日报</h3>'
            f'<ul>{recap_items}</ul><p class="muted">仅作二手参考，已从主题、关键发言和商机证据中排除。</p></section>'
            if recap_items else ""
        )
        reason_html = f"<div><dt>关注</dt><dd>{html_escape(reason_text)}</dd></div>" if reason_text else ""
        action_html = (
            f"<div><dt>下一步</dt><dd>{html_escape(row.get('建议动作') or '')}</dd></div>"
            if is_action else ""
        )
        threads_html = (
            '<h3 class="section-title">群内大事</h3>' + "".join(thread_blocks)
            if thread_blocks else '<p class="muted">当前窗口没有可展开的有效讨论段。</p>'
        )
        group_blocks.append(
            f"<details class=\"group-row\" data-kind=\"{row_kind}\" "
            f"data-search=\"{html_escape(search_text, quote=True)}\">"
            "<summary>"
            f"<span class=\"group-name\">{html_escape(row.get('群聊', ''))}</span>"
            f"<span class=\"badges\">{badge_html}</span>"
            f"<span class=\"stats\">{row.get('消息数', '0')} 条 · 有效 {row.get('有效消息数', row.get('消息数', '0'))} 条 · "
            f"{row.get('发言人数', '0')} 人</span>"
            "</summary>"
            "<div class=\"group-body\">"
            f"<p class=\"lead\">{html_escape(group_digest_summary(row))}</p>"
            f"<div class=\"topic-tags\" aria-label=\"热门主题\">{topic_badges}</div>"
            f"<dl><div><dt>最后消息</dt><dd>{html_escape(row.get('最后消息时间') or '')}</dd></div>"
            f"{reason_html}{action_html}"
            "</dl>"
            f"{recap_html}{threads_html}"
            "</div></details>"
        )

    link_items = "".join(
        "<li>"
        f'<a href="{html_escape(safe_href(str(row.get("链接") or "")) or "#", quote=True)}" target="_blank" rel="noopener noreferrer">'
        f'{html_escape(shorten(str(row.get("链接") or ""), 90))}</a>'
        f'<span>{html_escape(str(row.get("相关群聊") or ""))}｜{html_escape(str(row.get("判断依据") or ""))}</span>'
        "</li>"
        for row in (high_links + suspected_links)[:15]
    )
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>微信群聊日报</title>
<style>
:root {{ color-scheme: light; --ink:#1f2328; --muted:#667085; --line:#d9dee7; --bg:#f6f7f9; --surface:#fff; --blue:#175cd3; --green:#067647; --amber:#b54708; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; letter-spacing:0; }}
main {{ width:min(1080px, calc(100% - 32px)); margin:0 auto; padding:32px 0 56px; }}
header {{ border-bottom:1px solid var(--line); padding-bottom:22px; margin-bottom:22px; }}
h1 {{ font-size:30px; line-height:1.25; margin:0 0 8px; }}
h2 {{ font-size:19px; margin:28px 0 12px; }}
h3 {{ font-size:15px; margin:0 0 5px; }}
p {{ margin:7px 0; }}
.meta,.muted {{ color:var(--muted); }}
.metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:18px 0; }}
.metric {{ background:var(--surface); border:1px solid var(--line); border-radius:6px; padding:13px 14px; }}
.metric strong {{ display:block; font-size:22px; line-height:1.2; }}
.metric span {{ color:var(--muted); font-size:13px; }}
.toolbar {{ position:sticky; top:0; z-index:2; display:flex; gap:10px; flex-wrap:wrap; align-items:center; padding:12px 0; background:var(--bg); }}
input[type=search] {{ flex:1 1 280px; min-height:40px; border:1px solid #b8c0cc; border-radius:6px; padding:8px 12px; font:inherit; background:#fff; }}
.segments {{ display:flex; border:1px solid #b8c0cc; border-radius:6px; overflow:hidden; background:#fff; }}
.segments button {{ min-height:38px; border:0; border-right:1px solid #b8c0cc; padding:0 13px; background:#fff; color:var(--ink); font:inherit; cursor:pointer; }}
.segments button:last-child {{ border-right:0; }}
.segments button[aria-pressed=true] {{ background:#eaf1ff; color:var(--blue); font-weight:600; }}
.group-row {{ background:var(--surface); border:1px solid var(--line); border-radius:6px; margin:8px 0; overflow:hidden; }}
.group-row[data-kind=action] {{ border-left:4px solid var(--amber); }}
.group-row[data-kind=important] {{ border-left:4px solid var(--green); }}
summary {{ min-height:56px; padding:14px 16px; display:grid; grid-template-columns:minmax(180px,1fr) auto auto; gap:12px; align-items:center; cursor:pointer; list-style:none; }}
summary::-webkit-details-marker {{ display:none; }}
.group-name {{ font-weight:700; min-width:0; overflow-wrap:anywhere; }}
.badges {{ display:flex; gap:5px; flex-wrap:wrap; justify-content:flex-end; }}
.badges span {{ border:1px solid #c8d7f0; color:#164b88; background:#f3f7fd; border-radius:999px; padding:1px 7px; font-size:12px; white-space:nowrap; }}
.topic-tags {{ display:flex; gap:6px; flex-wrap:wrap; margin:10px 0 14px; }}
.topic-tags span {{ border:1px solid #c9ead9; color:#067647; background:#f0faf5; border-radius:4px; padding:2px 8px; font-size:12px; }}
.stats {{ color:var(--muted); font-size:13px; white-space:nowrap; }}
.group-body {{ border-top:1px solid var(--line); padding:16px; }}
.lead {{ font-size:16px; }}
dl {{ margin:12px 0 16px; }}
dl div {{ display:grid; grid-template-columns:76px 1fr; gap:10px; padding:5px 0; }}
dt {{ color:var(--muted); }} dd {{ margin:0; }}
.thread {{ border-top:1px solid #e8ebf0; padding:13px 0 5px; }}
.section-title {{ margin-top:18px; }}
.recaps {{ border-top:1px solid #e8ebf0; margin-top:14px; padding-top:13px; }}
.recaps ul {{ margin:7px 0; padding-left:20px; }}
.evidence {{ margin:8px 0; padding-left:20px; }} .evidence time {{ color:var(--muted); }}
.links-note {{ padding:14px 16px; border-left:4px solid var(--green); background:#fff; }}
.links-note ul {{ margin:10px 0 0; padding-left:20px; }} .links-note li {{ margin:8px 0; }} .links-note li span {{ display:block; color:var(--muted); font-size:12px; }}
.coverage-warning {{ padding:12px 14px; border-left:4px solid var(--amber); background:#fff7ed; }}
a {{ color:var(--blue); }}
[hidden] {{ display:none !important; }}
@media (max-width:720px) {{ main {{ width:min(100% - 20px,1080px); padding-top:20px; }} .metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} summary {{ grid-template-columns:1fr; gap:6px; }} .badges {{ justify-content:flex-start; }} .stats {{ white-space:normal; }} }}
</style>
</head>
<body><main>
<header><h1>微信群聊日报</h1><p class="meta">{html_escape(since)} 至 {html_escape(until)} · {len(rows)} 个活跃群聊</p></header>
{coverage_warning}
<section class="metrics" aria-label="日报指标">
<div class="metric"><strong>{len(action_names)}</strong><span>需要处理或核实</span></div>
<div class="metric"><strong>{sum(1 for row in rows if _digest_int(row, '重要讨论数'))}</strong><span>有重点讨论的群</span></div>
<div class="metric"><strong>{len(high_links)}</strong><span>高概率跨群信号</span></div>
<div class="metric"><strong>{len(suspected_links)}</strong><span>待核实链接</span></div>
<div class="metric"><strong>{recap_count}</strong><span>群内已有日报</span></div>
</section>
<h2>按群查看</h2>
<div class="toolbar"><input id="search" type="search" placeholder="搜索群名、主题或信号" aria-label="搜索群聊"><div class="segments" role="group" aria-label="筛选"><button data-filter="focus" aria-pressed="true">重点</button><button data-filter="action" aria-pressed="false">需处理</button><button data-filter="all" aria-pressed="false">全部</button></div></div>
<div id="groups">{''.join(group_blocks)}</div>
<h2>商单雷达链接</h2><div class="links-note">保留高概率 {len(high_links)} 个、待核实 {len(suspected_links)} 个；同一 URL 只列一次。<ul>{link_items or '<li>本窗口没有达到门槛的链接信号。</li>'}</ul></div>
</main>
<script>
const rows=[...document.querySelectorAll('.group-row')], search=document.querySelector('#search'), buttons=[...document.querySelectorAll('[data-filter]')]; let filter='focus';
function apply(){{const q=search.value.trim().toLowerCase(); rows.forEach(row=>{{const kindMatch=filter==='all'||row.dataset.kind===filter||(filter==='focus'&&row.dataset.kind!=='background'); row.hidden=!(kindMatch&&(!q||row.dataset.search.includes(q)));}});}}
search.addEventListener('input',apply); buttons.forEach(button=>button.addEventListener('click',()=>{{filter=button.dataset.filter; buttons.forEach(item=>item.setAttribute('aria-pressed',String(item===button))); apply();}}));
apply();
</script></body></html>"""
    path.write_text(protect_document(html), encoding="utf-8")


def clean_url(raw_url: str) -> str:
    return raw_url.strip().rstrip(".,;:!?，。！？；、）)]}")


def normalize_url(raw_url: str) -> str:
    url = clean_url(raw_url)
    parts = urlsplit(url)
    scheme = "https"
    netloc = parts.netloc.lower()
    if netloc in {"twitter.com", "mobile.twitter.com"}:
        netloc = "x.com"
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = re.sub(r"/+", "/", parts.path).rstrip("/")

    status_match = re.match(r"^/([^/]+)/status/(\d+)", path)
    if netloc in {"x.com", "twitter.com"} and status_match:
        path = f"/{status_match.group(1)}/status/{status_match.group(2)}"
        return urlunsplit((scheme, "x.com", path, "", ""))

    keep_query_keys = {"id", "doc", "token", "v", "list", "__biz", "mid", "idx", "sn"}
    query_items = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key in keep_query_keys]
    query = urlencode(query_items)
    return urlunsplit((scheme, netloc, path, query, ""))


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in URL_PATTERN.findall(text or ""):
        normalized = normalize_url(match)
        host = urlsplit(normalized).hostname or ""
        if host in {"wxapp.tc.qq.com", "vweixinf.tc.qq.com", "support.weixin.qq.com"} or host.endswith(".qpic.cn") or host == "qpic.cn" or host.endswith(".qlogo.cn"):
            continue
        if normalized and normalized not in urls:
            urls.append(normalized)
    return urls


def link_category(url: str, messages: list[Message]) -> str:
    host = urlsplit(url).netloc.lower()
    text = " ".join(message.content for message in messages)
    if host in {"x.com", "twitter.com"}:
        if MONETIZATION_TERMS.search(text):
            return "X 商单/加热链接"
        return "X 内容链接"
    if "docs.qq.com" in host or "feishu" in host or "notion" in host:
        return "Brief/文档"
    if "github.com" in host:
        return "GitHub/开源项目"
    if "youtube.com" in host or "youtu.be" in host:
        return "视频/课程"
    if MONETIZATION_TERMS.search(text):
        return "变现/合作链接"
    return "普通链接"


def has_high_follower_signal(messages: list[Message]) -> bool:
    for message in messages:
        sender = message.sender
        content = message.content
        if re.search(r"万粉|粉丝.{0,8}(?:1\s*[wW万]|10\s*[kK])", f"{sender} {content}", re.I):
            return True
        contextual = re.findall(
            r"(?:粉丝|粉丝量|蓝\s*[vV]|账号|博主).{0,10}(\d+(?:\.\d+)?)\s*([kKwW万])|"
            r"(\d+(?:\.\d+)?)\s*([kKwW万]).{0,8}(?:粉|粉丝|蓝\s*[vV]|账号|博主)",
            content,
            re.I,
        )
        sender_values = re.findall(r"(?<!\d)(\d+(?:\.\d+)?)\s*([kKwW万])", sender)
        for match in contextual:
            value, unit = (match[0], match[1]) if match[0] else (match[2], match[3])
            followers = float(value) * (1_000 if unit.lower() == "k" else 10_000)
            if followers >= 10_000:
                return True
        for value, unit in sender_values:
            followers = float(value) * (1_000 if unit.lower() == "k" else 10_000)
            if followers >= 10_000:
                return True
    return False


def assess_link_deal_probability(messages: list[Message], chat_count: int) -> tuple[str, str, int]:
    paid_boost_count = sum(1 for message in messages if PAID_BOOST_TERMS.search(message.content))
    explicit_non_deal_count = sum(1 for message in messages if EXPLICIT_NON_DEAL_TERMS.search(message.content))
    high_follower = has_high_follower_signal(messages)

    if explicit_non_deal_count:
        return "明确非商单", "原文明确写了非商单/纯分享", 0
    if chat_count >= 2 and paid_boost_count > 0:
        paid_chats = len({message.chat for message in messages if PAID_BOOST_TERMS.search(message.content)})
        reason = f"同链接跨{chat_count}个群出现，其中{paid_chats}个群有付费/红包加热信号"
        if high_follower:
            reason += "，且有万粉以上博主信号"
        return "高概率商单", reason, 3 if high_follower else 2
    if chat_count >= 3 and high_follower:
        return "高概率商单", f"同链接跨{chat_count}个群传播，且有万粉以上博主信号", 2
    if paid_boost_count > 0:
        return "疑似商单", "存在付费/红包加热，但跨群样本不足", 1
    if chat_count >= 2:
        return "疑似商单", f"同一推广链接在{chat_count}个群出现", 1
    return "普通内容", "暂无足够商单信号", 0


def is_social_profile_url(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    path_parts = [part for part in parts.path.split("/") if part]
    return host in {"x.com", "twitter.com", "mobile.twitter.com"} and len(path_parts) == 1


def is_owner_social_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.netloc.lower().removeprefix("www.") not in {"x.com", "twitter.com", "mobile.twitter.com"}:
        return False
    path_parts = [part.lower() for part in parts.path.split("/") if part]
    if not path_parts:
        return False
    handles = {item.lower().lstrip("@") for item in profile_list("owner_social_handles")}
    return path_parts[0] in handles


def build_link_rows(grouped: dict[str, list[Message]], min_chats: int = 2) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for url, link_messages in grouped.items():
        chats = sorted({message.chat for message in link_messages})
        has_direct_opportunity = any(
            GROUP_DIGEST_ACTIONABLE_DEAL_TERMS.search(message.content)
            or GROUP_DIGEST_ACTIONABLE_TRAINING_TERMS.search(message.content)
            or GROUP_DIGEST_ACTIONABLE_PROJECT_TERMS.search(message.content)
            for message in link_messages
        )
        if len(chats) < min_chats and not has_direct_opportunity:
            continue
        sorted_messages = sorted(link_messages, key=lambda message: message.time or "")
        if is_social_profile_url(url):
            assessment, reason, probability_rank = (
                "普通内容",
                "这是账号主页，不是可归因的推广帖或活动页",
                0,
            )
        else:
            assessment, reason, probability_rank = assess_link_deal_probability(link_messages, len(chats))
        opportunity_count = sum(
            1
            for message in link_messages
            if not EXPLICIT_NON_DEAL_TERMS.search(message.content)
            and (MONETIZATION_TERMS.search(message.content) or PAID_BOOST_TERMS.search(message.content) or AMOUNT_PATTERN.search(message.content))
        )
        snippets = " | ".join(unique_items(f"{message.chat}/{message.sender}: {shorten(message.content, 88)}" for message in sorted_messages)[:4])
        category = link_category(url, link_messages)
        if assessment == "高概率商单":
            category = "高概率商单/加热链接"
        elif assessment == "疑似商单":
            category = "疑似商单/加热链接"
        elif assessment == "明确非商单":
            category = "明确非商单内容"
        rows.append(
            {
                "链接": url,
                "类型": category,
                "商单判断": assessment,
                "判断依据": reason,
                "商单概率等级": str(probability_rank),
                "出现次数": str(len(link_messages)),
                "覆盖群数": str(len(chats)),
                "相关群聊": "、".join(chats),
                "机会信号数": str(opportunity_count),
                "首次出现": sorted_messages[0].time,
                "最后出现": sorted_messages[-1].time,
                "证据摘要": snippets,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            int(row["商单概率等级"]),
            int(row["覆盖群数"]),
            int(row["机会信号数"]),
            int(row["出现次数"]),
            row["最后出现"],
        ),
        reverse=True,
    )


def aggregate_links(messages: list[Message], min_chats: int = 2) -> list[dict[str, str]]:
    grouped: dict[str, list[Message]] = {}
    for message in messages:
        for url in extract_urls(message.content):
            grouped.setdefault(url, []).append(message)
    return build_link_rows(grouped, min_chats=min_chats)


def apply_cross_group_deal_signals(summaries: list[dict[str, str]], link_rows: list[dict[str, str]]) -> None:
    high_counts: Counter[str] = Counter()
    suspected_counts: Counter[str] = Counter()
    for row in link_rows:
        chats = [chat for chat in row.get("相关群聊", "").split("、") if chat]
        if row.get("商单判断") == "高概率商单":
            high_counts.update(chats)
        elif row.get("商单判断") == "疑似商单":
            suspected_counts.update(chats)

    for summary in summaries:
        chat = summary["群聊"]
        high_count = high_counts.get(chat, 0)
        suspected_count = suspected_counts.get(chat, 0)
        summary["跨群高概率商单"] = str(high_count)
        summary["跨群疑似商单"] = str(suspected_count)


def format_link_row_for_digest(row: dict[str, str]) -> str:
    labels = [row.get("商单判断") or row["类型"], f"{row['覆盖群数']}群/{row['出现次数']}次"]
    if int(row["机会信号数"]) > 0:
        labels.append(f"机会信号 {row['机会信号数']}")
    return f"- **{'｜'.join(labels)}**：{row['链接']}  \n  出现群：{row['相关群聊']}"


def write_cross_group_link_report(out_dir: Path, rows: list[dict[str, str]], since: str, until: str) -> None:
    write_csv(
        out_dir / "cross_group_links.csv",
        rows,
        ["链接", "类型", "商单判断", "判断依据", "商单概率等级", "出现次数", "覆盖群数", "相关群聊", "机会信号数", "首次出现", "最后出现", "证据摘要"],
    )
    repeated = [
        row for row in rows
        if int(row["覆盖群数"]) >= 2 and not is_owner_social_url(row["链接"])
    ]
    monetizable = [row for row in repeated if int(row["机会信号数"]) > 0]
    lines = [
        "# 跨群重复链接和机会聚合",
        "",
        f"- 时间范围：{since} 至 {until}",
        f"- 符合展示门槛：{len(repeated)} 个",
        f"- 跨群重复链接：{len(repeated)} 个",
        f"- 带商单/变现信号链接：{len(monetizable)} 个",
        "",
        "## 链接清单",
        "",
    ]
    if not repeated:
        lines.append("暂无跨群重复链接或带商单/变现信号的链接。")
    for row in repeated[:20]:
        lines.append(format_link_row_for_digest(row))
    (out_dir / "cross_group_links.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def message_hash(message: Message) -> str:
    raw = "\n".join([message.chat, message.sender, message.time, message.content])
    if source_chat_id(message.source_file):
        raw += "\nchat-id:" + source_chat_id(message.source_file)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


PIPELINE_CONTEXT_TERMS = re.compile(
    r"合作|商单|推广|投放|品牌方|项目方|报价|预算|brief|排期|发布时间|"
    r"初稿|审核|修改|发布|结算|付款|打款|收款|invoice|payment|"
    r"培训|工作坊|讲师|授课|咨询项目|项目合作|"
    r"浏览量|曝光|KOL|博主转发|达人转发|补量|加热|数据统计|数据回传|回传数据",
    re.I,
)
GROUP_ACTIONABLE_DEAL_TERMS = re.compile(
    r"商单|谁想接|有人想接|想接|可接|接单|招募|名额|brief|"
    r"返佣|佣金|CPS|CPA|"
    r"(?:品牌方|项目方).{0,16}(?:招|找|需要|合作|投放|预算|名额|发)|"
    r"(?:招|找|需要|合作|投放|预算|名额).{0,16}(?:品牌方|项目方)|"
    r"(?:合作|campaign|sponsor).{0,16}(?:机会|招募|预算|报价|名额|报名|找人|博主|达人)|"
    r"(?:有|给|确认|需要|可谈).{0,10}(?:预算|报价)|"
    r"找.{0,10}(?:KOL|达人|博主)|想找.{0,12}(?:发|推|合作)",
    re.I,
)
GROUP_ACTIONABLE_TRAINING_TERMS = re.compile(
    r"(?:有没有|谁|需要|招募?|找|寻找?|推荐|想搞|准备|计划|有个|有一场|接)"
    r".{0,20}(?:培训|讲师|课程|工作坊|项目|教练)|"
    r"(?:培训|讲师|课程|工作坊|项目|教练)"
    r".{0,20}(?:招募?|需要|找|寻找?|合作|报名|预算|报价|课时|授课|推荐|机会|需求)",
    re.I,
)
def is_project_collaboration_group(chat: str, rows: list[Message]) -> bool:
    senders = {
        message.sender.strip()
        for message in rows
        if message.sender.strip() not in {"", "系统"}
    }
    has_pipeline_context = any(PIPELINE_CONTEXT_TERMS.search(message.content) for message in rows)
    name_markers = profile_list("project_chat_terms") + configured_self_names()
    named_for_delivery = any(marker.casefold() in chat.casefold() for marker in name_markers)
    address_aliases = [alias for alias in configured_self_names() if alias.casefold() not in {"我", "me", "自己"}]
    directly_addresses_owner = any(
        re.search(rf"(?:@\s*)?{re.escape(alias)}\s*(?:老师)?", message.content, re.I)
        for alias in address_aliases
        for message in rows
    )
    return has_pipeline_context and len(senders) <= 12 and (named_for_delivery or directly_addresses_owner)


def build_opportunity_candidates(
    messages: list[Message],
    known_group_chats_seed: set[str] | None = None,
) -> list[dict[str, Any]]:
    timeline_identity_pattern = re.compile(r"timeline:([^:]+)$")
    known_group_chats: set[str] = set(known_group_chats_seed or set())
    known_group_identities: dict[str, set[str]] = {}
    known_private_identities: dict[str, set[str]] = {}
    source_identities: dict[int, str] = {}
    for message in messages:
        match = timeline_identity_pattern.search(message.source_file)
        identity = match.group(1) if match else ""
        source_identities[id(message)] = identity
        if identity.endswith("@chatroom"):
            known_group_chats.add(message.chat)
            known_group_identities.setdefault(message.chat, set()).add(identity)
        elif identity:
            known_private_identities.setdefault(message.chat, set()).add(identity)

    def resolved_group_identity(message: Message) -> str:
        identity = source_identities[id(message)]
        if not identity and len(known_group_identities.get(message.chat, set())) == 1:
            identity = next(iter(known_group_identities[message.chat]))
        return identity or message.chat

    group_url_sources: dict[str, set[str]] = {}
    for message in messages:
        is_group = message.chat in known_group_chats or source_identities[id(message)].endswith("@chatroom")
        if (
            not is_group
            or message.sender == "系统"
            or is_owner_sender(message.sender)
            or is_noise_evidence(message.content)
        ):
            continue
        for url in extract_urls(message.content):
            if PAID_BOOST_TERMS.search(message.content):
                group_url_sources.setdefault(url, set()).add(resolved_group_identity(message))

    group_rows_by_chat: dict[str, list[Message]] = {}
    for message in messages:
        if message.chat in known_group_chats or source_identities[id(message)].endswith("@chatroom"):
            group_rows_by_chat.setdefault(message.chat, []).append(message)
    project_collaboration_chats = {
        chat
        for chat, rows in group_rows_by_chat.items()
        if is_project_collaboration_group(chat, rows)
    }

    private_groups: dict[tuple[str, str], list[Message]] = {}
    group_anchors: dict[tuple[str, str], list[Message]] = {}
    for message in messages:
        is_group = message.chat in known_group_chats or source_identities[id(message)].endswith("@chatroom")
        is_deal = is_deal_opportunity(message)
        is_training = is_training_project_opportunity(message)
        if is_group and message.chat not in project_collaboration_chats:
            if (
                message.sender == "系统"
                or is_owner_sender(message.sender)
                or is_noise_evidence(message.content)
                or EXPLICIT_NON_DEAL_TERMS.search(message.content)
                or re.search(r"不是\s*(?:campaign|商单|合作|推广)", message.content, re.I)
                or is_group_recap_message(message)
            ):
                continue
            urls = extract_urls(message.content)
            repeated_paid_link = any(len(group_url_sources.get(url, set())) >= 2 for url in urls)
            explicit_opportunity = bool(
                GROUP_ACTIONABLE_DEAL_TERMS.search(message.content)
                or GROUP_ACTIONABLE_TRAINING_TERMS.search(message.content)
            )
            if not (explicit_opportunity or repeated_paid_link):
                continue
            anchor = urls[0] if urls else evidence_dedupe_key(message.content)
            anchor_digest = hashlib.sha256(anchor.encode("utf-8")).hexdigest()[:20]
            scope = "url" if urls else resolved_group_identity(message)
            group_anchors.setdefault((scope, anchor_digest), []).append(message)
            continue
        if not (is_deal or is_training or PIPELINE_CONTEXT_TERMS.search(message.content)):
            continue
        private_identity = source_identities[id(message)]
        if not private_identity and len(known_private_identities.get(message.chat, set())) == 1:
            private_identity = next(iter(known_private_identities[message.chat]))
        if message.chat in project_collaboration_chats:
            private_identity = f"project:{private_identity or message.chat}"
        else:
            private_identity = private_identity or f"name:{message.chat}"
        private_groups.setdefault((private_identity, message.chat), []).append(message)

    candidates: list[dict[str, Any]] = []

    def make_candidate(
        opportunity_key: str,
        chat: str,
        rows: list[Message],
        *,
        group_source: bool,
        project_collaboration: bool = False,
    ) -> dict[str, Any]:
        rows = sorted(dedupe_messages(rows), key=lambda message: message.time or "")
        signals = extract_signals(rows)
        training_rows = [message for message in rows if is_training_project_opportunity(message)]
        deal_rows = [message for message in rows if is_deal_opportunity(message)]
        if training_rows:
            opportunity_type = "培训/咨询/项目合作"
        elif deal_rows:
            opportunity_type = "商单/推广"
        else:
            opportunity_type = "其他合作"

        if group_source:
            stage = "新线索"
            priority = max([signal.priority for signal in signals] or [5 if training_rows else 4])
            amount = unique_join(
                " / ".join(clean_amounts(AMOUNT_PATTERN.findall(message.content)))
                for message in rows
                if re.search(r"预算|报价|稿费|佣金|保底|课时费|授课费", message.content, re.I)
            )
        elif signals:
            stage = max(signals, key=lambda signal: stage_rank(signal.stage)).stage
            priority = max(signal.priority for signal in signals)
            amount = unique_join(signal.amount for signal in signals if signal.amount)
        else:
            stage = "新线索"
            priority = 5 if training_rows else 3
            amount = unique_join(
                " / ".join(clean_amounts(AMOUNT_PATTERN.findall(message.content)))
                for message in rows
            )

        text = " ".join(message.content for message in rows)
        source_chats = unique_items(message.chat for message in rows)
        explicit_request = bool(
            GROUP_ACTIONABLE_DEAL_TERMS.search(text)
            or GROUP_ACTIONABLE_TRAINING_TERMS.search(text)
        )
        has_budget = bool(re.search(r"预算|报价|稿费|佣金|保底|课时费|授课费", text, re.I))
        has_deadline = bool(DISCUSSION_DEADLINE_TERMS.search(text))
        repeated_distribution = group_source and len(source_chats) >= 2
        reasons: list[str] = []
        score = 0
        if explicit_request:
            score += 35
            reasons.append("明确招募或合作需求")
        if has_budget:
            score += 20
            reasons.append("包含预算或报价")
        if has_deadline:
            score += 10
            reasons.append("包含时间节点")
        if repeated_distribution:
            score += 30
            reasons.append("跨群重复投放")
        if project_collaboration:
            score += 30
            reasons.append("直接交付或合作群")
        if not group_source and stage != "新线索":
            score += 35
            reasons.append(f"私聊已进入{stage}")
        if training_rows and explicit_request:
            score += 15
            reasons.append("明确培训或项目需求")
        score = min(score, 100)
        confidence = "high" if score >= 60 else "medium" if score >= 40 else "low"
        record_type = (
            "opportunity"
            if project_collaboration or (not group_source and stage != "新线索" and score >= 55)
            else "candidate"
        )

        title = chat
        if group_source:
            source_label = "、".join(source_chats[:3])
            if len(source_chats) > 3:
                source_label += f"等{len(source_chats)}群"
            title = f"{source_label}｜{shorten(rows[-1].content, 42)}"
            chat = source_label
        return {
            "opportunity_key": opportunity_key,
            "chat": chat,
            "title": title,
            "opportunity_type": opportunity_type,
            "stage": stage,
            "priority": priority,
            "amount": amount,
            "last_signal_time": rows[-1].time,
            "next_action": (
                "回原群核实品牌、预算、名额和对接人"
                if group_source
                else infer_next_action(stage)
            ),
            "replace_amount": group_source,
            "project_collaboration": project_collaboration,
            "record_type": record_type,
            "confidence": confidence,
            "qualification_score": score,
            "qualification_reasons": "；".join(reasons) or "仅有弱上下文，需人工核实",
            "message_hashes": [message_hash(message) for message in rows],
        }

    for (identity, chat), rows in private_groups.items():
        identity_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        candidates.append(
            make_candidate(
                f"private:{identity_digest}",
                chat,
                rows,
                group_source=False,
                project_collaboration=identity.startswith("project:"),
            )
        )
    for (group_scope, anchor_digest), rows in group_anchors.items():
        group_digest = hashlib.sha256(group_scope.encode("utf-8")).hexdigest()[:16]
        candidates.append(
            make_candidate(
                f"group:{group_digest}:{anchor_digest}",
                rows[-1].chat,
                rows,
                group_source=True,
            )
        )
    return candidates


def load_known_group_chats(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        select distinct chat
        from messages
        where source_file like '%timeline:%@chatroom'
        """
    ).fetchall()
    return {str(row["chat"]) for row in rows if row["chat"]}


def connect_radar_db(path_value: str | None) -> sqlite3.Connection:
    db_path = Path(path_value or DEFAULT_RADAR_DB).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma busy_timeout=15000")
    conn.execute("pragma synchronous=normal")
    conn.execute("pragma foreign_keys=on")
    return conn


def connect_readonly_radar_db(path_value: str) -> sqlite3.Connection:
    db_path = Path(path_value).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys=on")
    conn.execute("pragma busy_timeout=15000")
    return conn


def init_radar_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists runs (
            id integer primary key autoincrement,
            run_type text not null,
            since_time text not null,
            until_time text not null,
            created_at text not null,
            groups_count integer not null default 0,
            messages_count integer not null default 0,
            output_dir text not null default ''
        );

        create table if not exists messages (
            id integer primary key autoincrement,
            hash text not null unique,
            run_id integer,
            chat text not null,
            sender text not null,
            time text not null,
            content text not null,
            source_file text not null,
            created_at text not null,
            foreign key(run_id) references runs(id)
        );

        create virtual table if not exists messages_fts using fts5(
            chat,
            sender,
            content,
            time unindexed,
            source_file unindexed,
            tokenize='unicode61'
        );

        create table if not exists group_summaries (
            id integer primary key autoincrement,
            run_id integer not null,
            chat text not null,
            message_count integer not null default 0,
            speaker_count integer not null default 0,
            active_speakers text not null default '',
            last_message_time text not null default '',
            topics text not null default '',
            signal_count integer not null default 0,
            opportunity_count integer not null default 0,
            stage text not null default '',
            next_action text not null default '',
            timeline_summary text not null default '',
            key_messages text not null default '',
            opportunity_summary text not null default '',
            created_at text not null,
            foreign key(run_id) references runs(id)
        );

        create table if not exists message_links (
            id integer primary key autoincrement,
            message_id integer not null,
            url text not null,
            category text not null default '',
            created_at text not null,
            unique(message_id, url),
            foreign key(message_id) references messages(id)
        );

        create index if not exists idx_message_links_url on message_links(url);
        """
    )
    init_opportunity_schema(conn)
    conn.commit()


def backfill_message_links(conn: sqlite3.Connection) -> int:
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """
        select id, chat, sender, time, content, source_file
        from messages m
        where content like '%http%'
          and not exists (select 1 from message_links l where l.message_id = m.id)
        """
    ).fetchall()
    inserted = 0
    with conn:
        for row in rows:
            message = Message(
                chat=str(row["chat"]),
                sender=str(row["sender"]),
                time=str(row["time"]),
                content=str(row["content"]),
                source_file=str(row["source_file"]),
            )
            for url in extract_urls(message.content):
                cursor = conn.execute(
                    "insert or ignore into message_links(message_id, url, category, created_at) values (?, ?, ?, ?)",
                    (int(row["id"]), url, link_category(url, [message]), created_at),
                )
                inserted += cursor.rowcount
    return inserted


def insert_messages_to_db(conn: sqlite3.Connection, run_id: int, messages: list[Message], created_at: str) -> tuple[int, int]:
    inserted_messages = 0
    inserted_links = 0
    for message in messages:
        digest = message_hash(message)
        existing = conn.execute("select id from messages where hash = ?", (digest,)).fetchone()
        if not existing and source_chat_id(message.source_file):
            legacy_digest = hashlib.sha256("\n".join([
                message.chat, message.sender, message.time, message.content,
            ]).encode("utf-8")).hexdigest()
            legacy = conn.execute(
                "select id, source_file from messages where hash = ?", (legacy_digest,),
            ).fetchone()
            # Upgrade only a proven same-conversation row, retaining IDs used by
            # FTS and link evidence. Ambiguous imports are not silently merged.
            if legacy and source_chat_id(str(legacy["source_file"])) == source_chat_id(message.source_file):
                conn.execute("update messages set hash = ? where id = ?", (digest, legacy["id"]))
                existing = legacy
        if existing:
            message_id = int(existing["id"])
        else:
            insert_cursor = conn.execute(
                """
                insert or ignore into messages(hash, run_id, chat, sender, time, content, source_file, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (digest, run_id, message.chat, message.sender, message.time, message.content, message.source_file, created_at),
            )
            if insert_cursor.rowcount:
                message_id = int(insert_cursor.lastrowid)
                inserted_messages += 1
                conn.execute(
                    "insert into messages_fts(rowid, chat, sender, content, time, source_file) values (?, ?, ?, ?, ?, ?)",
                    (message_id, message.chat, message.sender, message.content, message.time, message.source_file),
                )
            else:
                message_id = int(conn.execute("select id from messages where hash = ?", (digest,)).fetchone()["id"])
        for url in extract_urls(message.content):
            cursor = conn.execute(
                "insert or ignore into message_links(message_id, url, category, created_at) values (?, ?, ?, ?)",
                (message_id, url, link_category(url, [message]), created_at),
            )
            inserted_links += cursor.rowcount
    return inserted_messages, inserted_links


def store_messages_to_db(
    db_path: str | None,
    run_type: str,
    since: str | None,
    until: str | None,
    messages: list[Message],
    output_dir: Path,
) -> tuple[Path, int, int]:
    conn = connect_radar_db(db_path)
    init_radar_db(conn)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        cursor = conn.execute(
            """
            insert into runs(run_type, since_time, until_time, created_at, groups_count, messages_count, output_dir)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_type, since or "", until or "", created_at, len({message.chat for message in messages}), len(messages), str(output_dir)),
        )
        run_id = int(cursor.lastrowid)
        inserted_messages, inserted_links = insert_messages_to_db(conn, run_id, messages, created_at)
        sync_opportunity_candidates(
            conn,
            run_id,
            build_opportunity_candidates(messages, load_known_group_chats(conn)),
            created_at,
        )
    db_file = Path(db_path or DEFAULT_RADAR_DB).expanduser()
    conn.close()
    return db_file, inserted_messages, inserted_links


def store_group_daily_to_db(
    db_path: str | None,
    since: str,
    until: str,
    summaries: list[dict[str, str]],
    messages: list[Message],
    output_dir: Path,
) -> Path:
    conn = connect_radar_db(db_path)
    init_radar_db(conn)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        cursor = conn.execute(
            """
            insert into runs(run_type, since_time, until_time, created_at, groups_count, messages_count, output_dir)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            ("group_daily", since, until, created_at, len(summaries), len(messages), str(output_dir)),
        )
        run_id = int(cursor.lastrowid)
        insert_messages_to_db(conn, run_id, messages, created_at)
        sync_opportunity_candidates(
            conn,
            run_id,
            build_opportunity_candidates(messages, load_known_group_chats(conn)),
            created_at,
        )
        for row in summaries:
            conn.execute(
                """
                insert into group_summaries(
                    run_id, chat, message_count, speaker_count, active_speakers, last_message_time,
                    topics, signal_count, opportunity_count, stage, next_action, timeline_summary,
                    key_messages, opportunity_summary, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    row["群聊"],
                    int(row["消息数"] or 0),
                    int(row["发言人数"] or 0),
                    row["活跃发言人"],
                    row["最后消息时间"],
                    row["主要主题"],
                    int(row["商单信号数"] or 0),
                    int(row["变现机会数"] or 0),
                    row["当前阶段"],
                    row["建议动作"],
                    row["群聊脉络"],
                    row["关键发言"],
                    row["商单/变现机会摘要"],
                    created_at,
                ),
            )
    db_file = Path(db_path or DEFAULT_RADAR_DB).expanduser()
    conn.close()
    return db_file


def db_search(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = args.query.strip()
    if re.search(r"[\u4e00-\u9fff]", query):
        sql = """
            select m.chat, m.sender, m.time, m.content, m.source_file
            from messages m
            where (m.content like ? or m.sender like ?)
        """
        like_query = f"%{query}%"
        params: list[Any] = [like_query, like_query]
    else:
        sql = """
            select m.chat, m.sender, m.time, m.content, m.source_file
            from messages_fts f
            join messages m on m.id = f.rowid
            where messages_fts match ?
        """
        params = [query]
    if args.chat:
        sql += " and m.chat like ?"
        params.append(f"%{args.chat}%")
    if args.since:
        sql += " and m.time >= ?"
        params.append(normalize_time(args.since))
    if args.until:
        sql += " and m.time <= ?"
        params.append(normalize_time(args.until))
    sql += " order by m.time desc limit ?"
    params.append(args.limit)
    rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    conn.close()

    out_path = Path(args.out).expanduser() if args.out else None
    lines = [f"# 微信本地情报库搜索：{query}", ""]
    for row in rows:
        lines.append(f"- **{row['time']}｜{row['chat']}｜{row['sender']}**：{shorten(row['content'], 180)}")
    text = "\n".join(lines) + "\n"
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"命中：{len(rows)} 条")
        print(f"输出文件：{out_path}")
    else:
        print(text)


def fetch_messages_for_contact(conn: sqlite3.Connection, names: list[str], since: str | None, limit: int) -> list[Message]:
    clean_names = unique_items(name.strip() for name in names if name.strip())
    if not clean_names:
        return []
    # 复联评分只看与该联系人的一对一会话。用 sender 模糊匹配会把共同群、
    # 短昵称（如 Ji）和其他人的消息混进来，污染沉默时长和合作阶段。
    clauses = ["lower(chat) = ?" for _ in clean_names]
    params: list[Any] = [name.lower() for name in clean_names]
    sql = """
        select chat, sender, time, content, source_file
        from messages
        where (
    """ + " or ".join(clauses) + ")"
    if since:
        sql += " and time >= ?"
        params.append(normalize_time(since))
    sql += " order by time desc limit ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        Message(
            chat=str(row["chat"]),
            sender=str(row["sender"]),
            time=str(row["time"]),
            content=str(row["content"]),
            source_file=str(row["source_file"]),
        )
        for row in rows
    ]


def fetch_messages_for_chats(conn: sqlite3.Connection, chats: list[str], since: str | None, limit: int) -> list[Message]:
    clean_chats = unique_items(chat.strip() for chat in chats if chat.strip())
    if not clean_chats:
        return []
    clauses = ["lower(chat) = ?" for _ in clean_chats]
    params: list[Any] = [chat.lower() for chat in clean_chats]
    sql = """
        select chat, sender, time, content, source_file
        from messages
        where (
    """ + " or ".join(clauses) + ")"
    if since:
        sql += " and time >= ?"
        params.append(normalize_time(since))
    sql += " order by time desc limit ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        Message(
            chat=str(row["chat"]),
            sender=str(row["sender"]),
            time=str(row["time"]),
            content=str(row["content"]),
            source_file=str(row["source_file"]),
        )
        for row in rows
    ]


def days_since_time(value: str | None, now: datetime | None = None) -> int | None:
    parsed = parse_time_for_filter(value)
    if not parsed:
        return None
    current = now or datetime.now()
    return max((current - parsed).days, 0)


def latest_matching_message(messages: list[Message], pattern: re.Pattern[str]) -> Message | None:
    for message in messages:
        if pattern.search(message.content):
            return message
    return None


def latest_reactivation_negative_message(messages: list[Message]) -> Message | None:
    for message in messages:
        content = message.content
        if not REACTIVATION_NEGATIVE_TERMS.search(content):
            continue
        if (
            REACTIVATION_HANDOFF_TERMS.search(content)
            or REACTIVATION_THIRD_PARTY_CONTEXT.search(content)
            or REACTIVATION_NEGATIVE_HYPOTHETICAL_CONTEXT.search(content)
        ):
            continue
        return message
    return None


def latest_partner_matching_message(
    messages: list[Message],
    pattern: re.Pattern[str],
    self_names: list[str] | None = None,
) -> Message | None:
    for message in messages:
        if not is_self_sender(message.sender, self_names) and pattern.search(message.content):
            return message
    return None


def latest_partner_defer_message(messages: list[Message], self_names: list[str] | None = None) -> Message | None:
    for index, candidate in enumerate(messages):
        if is_self_sender(candidate.sender, self_names) or not REACTIVATION_DEFER_TERMS.search(candidate.content):
            continue
        if REACTIVATION_VAGUE_WAIT_TERMS.search(candidate.content):
            nearby = messages[max(0, index - 3) : index + 4]
            if any(REACTIVATION_SETTLEMENT_CONTEXT_TERMS.search(message.content) for message in nearby):
                continue
        return candidate
    return None


def is_self_sender(sender: str, self_names: list[str] | None = None) -> bool:
    names = {name.casefold() for name in configured_self_names(self_names)}
    return sender.strip().casefold() in names


def self_sent_latest(messages: list[Message], self_names: list[str] | None = None) -> bool:
    if not messages:
        return False
    return is_self_sender(messages[0].sender, self_names)


def event_is_latest(candidate: Message | None, *others: Message | None) -> bool:
    if candidate is None:
        return False
    candidate_time = normalize_time(candidate.time)
    return all(other is None or candidate_time >= normalize_time(other.time) for other in others)


def defer_wait_days(message: Message | None) -> int:
    if message is None:
        return 14
    text = message.content
    if re.search(r"下一批|下批|下一轮|下轮|下次", text, re.I):
        return 7
    if re.search(r"需要一段时间|晚一段时间|要等等|再等等", text, re.I):
        return 14
    return 21


def follow_up_date(message: Message | None, wait_days: int) -> str:
    parsed = parse_time_for_filter(message.time if message else None)
    if not parsed:
        return ""
    return (parsed + timedelta(days=wait_days)).strftime("%Y-%m-%d")


def classify_reactivation(
    messages: list[Message],
    signals: list[Signal],
    inactive_days: int | None,
    threshold: int,
    self_names: list[str] | None = None,
) -> tuple[str, int, str]:
    if not messages:
        return "无聊天记录", 0, "先不主动处理；除非你确认这是重要品牌，再手动建联。"

    text = "\n".join(message.content for message in messages[:80])
    latest_self = self_sent_latest(messages, self_names)
    has_active = any(signal.stage in ACTIVE_STAGES for signal in signals)
    has_amount = any(signal.amount for signal in signals)
    has_asset = bool(REACTIVATION_ASSET_TERMS.search(text))
    latest_negative = latest_reactivation_negative_message(messages)
    latest_success = latest_matching_message(messages, REACTIVATION_SUCCESS_TERMS)
    latest_defer = latest_partner_defer_message(messages, self_names)
    latest_handoff = latest_partner_matching_message(messages, REACTIVATION_HANDOFF_TERMS, self_names)
    latest_commission_only = latest_partner_matching_message(messages, REACTIVATION_COMMISSION_ONLY_TERMS, self_names)
    has_settlement = latest_success is not None
    competing_events = [latest_negative, latest_success, latest_defer, latest_handoff, latest_commission_only]
    negative_is_current = event_is_latest(latest_negative, *[item for item in competing_events if item is not latest_negative])
    defer_is_current = event_is_latest(latest_defer, *[item for item in competing_events if item is not latest_defer])
    handoff_is_current = event_is_latest(latest_handoff, *[item for item in competing_events if item is not latest_handoff])
    commission_only_is_current = event_is_latest(
        latest_commission_only,
        *[item for item in competing_events if item is not latest_commission_only],
    )
    days = inactive_days if inactive_days is not None else 999

    score = 0
    if has_active:
        score += 38
    if has_settlement:
        score += 28
    if has_amount:
        score += 22
    if has_asset:
        score += 18
    if latest_self and days >= threshold:
        score += 14
    if not latest_self and days >= 7:
        score += 18
    if days >= threshold:
        score += 10
    if days >= 90:
        score += 6
    if negative_is_current:
        score -= 24
    if defer_is_current:
        score += 20
    if handoff_is_current:
        score += 16
    if commission_only_is_current:
        score -= 16
    score = max(score, 0)

    if handoff_is_current:
        return "待交接跟进", score, "原负责人已交接或不再负责，优先确认新负责人和承接群，不要继续只追旧联系人。"
    if defer_is_current:
        return "待下一批跟进", score, "对方明确留了下批/下次窗口，按日期提前跟进，不要等对方自然想起你。"
    if commission_only_is_current:
        return "纯佣低优先级", score, "当前是纯佣或无保底模式；除非产品转化强、数据透明，否则不占用主要商单时间。"
    if negative_is_current and latest_negative and is_self_sender(latest_negative.sender, self_names):
        return "我方主动放弃", score, "此前是你主动停止合作。先确认当时的产品风险或匹配问题是否已经解决，不要因为缺单直接恢复。"
    if negative_is_current and days < threshold:
        return "暂缓", score, "最近刚明确拒绝或预算不匹配，先留窗口，不要立刻再追。"
    if negative_is_current and (has_amount or has_asset or has_active):
        return "失败可修复", score, "之前卡在预算、效果或合作形式，复联时别硬推，先问新一轮规则。"
    if negative_is_current:
        return "暂缓", score, "历史里有明确低意向或不匹配信号，除非你有新案例再联系。"
    if has_settlement and days >= threshold:
        return "复购保温", score, "曾经推进到发布/结算/复盘，适合用近况和新排期唤起二次合作。"
    if has_active and days >= threshold:
        return "优先复联", score, "之前有明确合作信号但已经冷掉，建议今天发一条轻跟进。"
    if has_amount and has_asset and days >= threshold:
        return "可复联", score, "有预算/佣金/素材等合作资产，值得确认最新投放节奏。"
    if days < threshold:
        if latest_self:
            return "近期已联系", score, "最后一条是你发出的，先等对方回复；到跟进日期再轻提醒。"
        return "近期已联系", score, "对方最近有回复，先结合最后一条内容处理，不需要另发复联消息。"
    if signals:
        return "低频保温", score, "有合作相关记录但强度不高，可以排在后面轻触达。"
    return "低优先级", score, "暂时没看到明确商单信号。"


def reactivation_opening(row: dict[str, str]) -> str:
    status = row["复联类型"]
    name = row["联系人"]
    current_month = datetime.now().month
    next_month = 1 if current_month == 12 else current_month + 1
    month_window = f"{current_month} 月/{next_month} 月"
    if status == "优先复联":
        return f"{name}老师好，我这边最近在重新排 {month_window}内容档期，想问下你们这个项目现在还有投放或合作需求吗？如果有最新 brief/规则可以发我，我看下怎么结合我的内容来做。"
    if status == "复购保温":
        return f"{name}老师好，之前合作这边我最近也在整理复盘和新选题。你们近期如果有新一轮投放/活动，可以把节奏发我，我看看能不能排进最近的内容档期。"
    if status == "失败可修复":
        return f"{name}老师好，之前那次可能因为预算/形式没有完全对上。我最近账号内容和受众也有一些变化，想问下你们现在合作规则有没有更新？我可以按最新规则重新评估一下。"
    if status == "待下一批跟进":
        return f"{name}老师好，上次你说要等下一批，我来提前跟一下。下批如果开始排期，可以先把预算和合作形式发我，我给你们留一个档期。"
    if status == "待交接跟进":
        return f"{name}老师好，上次提到项目负责人有调整，想确认一下现在由哪位老师承接合作？方便的话可以拉个小群，我把之前谈过的报价和排期重新同步一下。"
    if status == "可复联":
        return f"{name}老师好，我最近在整理适合做长期合作的品牌和工具类项目，想问下你们这边现在还有合作/返佣/投放计划吗？有最新素材或规则的话可以发我看看。"
    if status == "低频保温":
        return f"{name}老师好，最近我在整理后续内容排期，顺手想问下你们这边近期有没有新的合作安排？如果暂时没有也没事，我先保持关注。"
    return ""


def build_reactivation_rows(
    targets: list[dict[str, Any]],
    conn: sqlite3.Connection,
    since: str | None,
    per_contact_limit: int,
    inactive_days: int,
    self_names: list[str] | None = None,
    handoff_relations: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    now = datetime.now()
    relations = handoff_relations or []
    for target in targets:
        handoff_contexts = target_handoff_contexts(target, relations)
        messages = fetch_messages_for_contact(conn, target["names"], since, per_contact_limit)
        group_chats = unique_items(
            value
            for context in handoff_contexts
            for value in [context["共同群ID"], context["共同群名"]]
            if value
        )
        if group_chats:
            messages.extend(fetch_messages_for_chats(conn, group_chats, since, per_contact_limit))
        messages = dedupe_messages(messages)
        messages = sorted(messages, key=lambda message: message.time or "", reverse=True)
        signals = extract_signals(messages)
        latest_time = messages[0].time if messages else ""
        inactive = days_since_time(latest_time, now)
        status, score, advice = classify_reactivation(messages, signals, inactive, inactive_days, self_names)
        old_owner_contexts = [context for context in handoff_contexts if context["角色"] == "原负责人"]
        if old_owner_contexts:
            status = "已交接"
            score = 0
            new_owners = "、".join(unique_items(context["新负责人"] for context in old_owner_contexts))
            groups = "、".join(unique_items(context["共同群名"] or context["共同群ID"] for context in old_owner_contexts))
            advice = f"已交接给 {new_owners}，后续统一在共同讨论组 {groups} 跟进，不再单独追原负责人。"
        latest_negative = latest_reactivation_negative_message(messages)
        latest_success = latest_matching_message(messages, REACTIVATION_SUCCESS_TERMS)
        latest_defer = latest_partner_defer_message(messages, self_names)
        latest_handoff = latest_partner_matching_message(messages, REACTIVATION_HANDOFF_TERMS, self_names)
        latest_commission_only = latest_partner_matching_message(messages, REACTIVATION_COMMISSION_ONLY_TERMS, self_names)
        latest_asset = latest_matching_message(messages, REACTIVATION_ASSET_TERMS)
        if status == "待下一批跟进":
            defer_anchor = latest_defer
            if messages and latest_defer and normalize_time(messages[0].time) > normalize_time(latest_defer.time):
                defer_anchor = messages[0]
            next_follow_up = follow_up_date(defer_anchor, defer_wait_days(latest_defer))
        elif status == "待交接跟进":
            handoff_anchor = messages[0] if messages else latest_handoff
            next_follow_up = follow_up_date(handoff_anchor, 7)
        elif status == "失败可修复" and latest_negative:
            next_follow_up = follow_up_date(latest_negative, inactive_days)
        elif status in {"已交接", "纯佣低优先级", "我方主动放弃"}:
            next_follow_up = ""
        else:
            next_follow_up = follow_up_date(messages[0] if messages else None, inactive_days)
        due_state = "无日期"
        if next_follow_up:
            due_state = "已到期" if next_follow_up <= now.strftime("%Y-%m-%d") else "未到期"
        latest_direction = ""
        if messages:
            latest_direction = "我最后发出" if is_self_sender(messages[0].sender, self_names) else "对方最后发来"
        if status == "待下一批跟进" and next_follow_up:
            advice = f"对方留了下批/下次窗口，建议 {next_follow_up} 主动跟进，不要等对方自然想起你。"
        elif status == "待交接跟进" and next_follow_up:
            advice = f"负责人发生变化，建议 {next_follow_up} 前确认新负责人或承接群，避免项目在线索交接中丢失。"
        elif status == "近期已联系" and next_follow_up:
            advice = f"{advice} 建议检查日期：{next_follow_up}。"
        amounts = unique_join(signal.amount for signal in signals if signal.amount)
        stages = Counter(signal.stage for signal in signals)
        stage_summary = " / ".join(f"{stage}:{count}" for stage, count in stages.most_common(4))
        evidence_messages = unique_items(
            f"{message.time[:10]} {message.sender}: {shorten(message.content, 88)}"
            for message in [latest_handoff, latest_defer, latest_commission_only, latest_success, latest_asset, latest_negative, *messages[:3]]
            if message
        )
        row = {
            "复联类型": status,
            "分数": str(score),
            "联系人": str(target["display_name"]),
            "标签": str(target["label"]),
            "最后聊天时间": latest_time,
            "最后一条方向": latest_direction,
            "沉默天数": str(inactive) if inactive is not None else "",
            "建议跟进日期": next_follow_up,
            "跟进状态": due_state,
            "消息数": str(len(messages)),
            "商单信号数": str(len(signals)),
            "阶段分布": stage_summary,
            "报价/佣金/预算": amounts,
            "交接关系": handoff_relation_summary(handoff_contexts),
            "建议动作": advice,
            "复联开场建议": "",
            "证据摘要": " | ".join(evidence_messages[:4]),
        }
        row["复联开场建议"] = reactivation_opening(row)
        rows.append(row)

    status_order = {
        "复购保温": 0,
        "待交接跟进": 1,
        "待下一批跟进": 2,
        "优先复联": 3,
        "可复联": 4,
        "失败可修复": 5,
        "低频保温": 6,
        "近期已联系": 7,
        "暂缓": 8,
        "已交接": 9,
        "我方主动放弃": 10,
        "纯佣低优先级": 11,
        "低优先级": 12,
        "无聊天记录": 13,
    }
    return sorted(
        rows,
        key=lambda row: (
            status_order.get(row["复联类型"], 14),
            0 if row["跟进状态"] == "已到期" else 1,
            -int(row["分数"] or 0),
            -(int(row["沉默天数"] or 0)),
            row["联系人"],
        ),
    )


def write_reactivation_report(path: Path, rows: list[dict[str, str]], labels: list[str], since: str | None, inactive_days: int) -> None:
    counts = Counter(row["复联类型"] for row in rows)
    actionable_statuses = {"优先复联", "复购保温", "可复联", "待交接跟进", "待下一批跟进", "失败可修复"}
    today = datetime.now().strftime("%Y-%m-%d")
    upcoming_end = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
    lines = [
        "# 品牌方复联雷达",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 标签范围：{'、'.join(labels)}",
        f"- 分析起点：{since or '不限'}",
        f"- 复联沉默阈值：{inactive_days} 天",
        f"- 覆盖联系人：{len(rows)} 个",
        f"- 优先复联：{counts.get('优先复联', 0)} 个",
        f"- 复购保温：{counts.get('复购保温', 0)} 个",
        f"- 待交接跟进：{counts.get('待交接跟进', 0)} 个",
        f"- 待下一批跟进：{counts.get('待下一批跟进', 0)} 个",
        f"- 失败可修复：{counts.get('失败可修复', 0)} 个",
        f"- 已交接：{counts.get('已交接', 0)} 个",
        f"- 我方主动放弃：{counts.get('我方主动放弃', 0)} 个",
        f"- 纯佣低优先级：{counts.get('纯佣低优先级', 0)} 个",
        "",
        "## 今天优先看",
        "",
    ]
    priority_rows = [
        row for row in rows
        if row["复联类型"] in actionable_statuses and row["跟进状态"] == "已到期"
    ]
    if not priority_rows:
        lines.append("今天没有到期的高优先级复联对象。")
    visible_priority_rows = priority_rows[:10]
    for row in visible_priority_rows:
        amount = f"，金额/规则：{row['报价/佣金/预算']}" if row["报价/佣金/预算"] else ""
        inactive = f"，沉默 {row['沉默天数']} 天" if row["沉默天数"] else ""
        lines.append(f"### {row['联系人']}｜{row['复联类型']}｜分数 {row['分数']}")
        lines.append("")
        lines.append(
            f"- 状态：最后聊天 {row['最后聊天时间'] or '未知'}，{row['最后一条方向'] or '方向未知'}{inactive}，"
            f"商单信号 {row['商单信号数']} 条{amount}"
        )
        if row["建议跟进日期"]:
            lines.append(f"- 跟进日期：{row['建议跟进日期']}（{row['跟进状态']}）")
        if row["阶段分布"]:
            lines.append(f"- 阶段：{row['阶段分布']}")
        if row["交接关系"]:
            lines.append(f"- 交接：{row['交接关系']}")
        lines.append(f"- 建议：{row['建议动作']}")
        if row["证据摘要"]:
            lines.append(f"- 证据：{row['证据摘要']}")
        if row["复联开场建议"]:
            lines.append(f"- 可发：{row['复联开场建议']}")
        lines.append("")
    if len(priority_rows) > len(visible_priority_rows):
        lines.append(f"另有 {len(priority_rows) - len(visible_priority_rows)} 个已到期候选保留在 CSV，本报告不继续展开。")
        lines.append("")

    upcoming_rows = [
        row for row in rows
        if row["跟进状态"] == "未到期"
        and row["建议跟进日期"]
        and today < row["建议跟进日期"] <= upcoming_end
        and row["复联类型"] in {"待交接跟进", "待下一批跟进", "暂缓"}
    ]
    upcoming_rows.sort(key=lambda row: (row["建议跟进日期"], -int(row["分数"] or 0)))
    lines.extend(["## 接下来 14 天", ""])
    if not upcoming_rows:
        lines.append("未来 14 天没有已排定的跟进提醒。")
        lines.append("")
    for row in upcoming_rows[:10]:
        lines.append(
            f"- **{row['建议跟进日期']}｜{row['联系人']}**：{row['复联类型']}，{row['最后一条方向'] or '方向未知'}。"
            f"{row['建议动作']}"
        )

    lines.extend(["## 暂缓/低优先级", ""])
    compact_rows = [item for item in rows if item not in priority_rows and item not in upcoming_rows]
    for row in compact_rows[:15]:
        lines.append(
            f"- **{row['联系人']}**：{row['复联类型']}，最后聊天 {row['最后聊天时间'] or '未知'}，"
            f"信号 {row['商单信号数']} 条。{row['建议动作']}"
        )
    if len(compact_rows) > 15:
        lines.append(f"- 另有 {len(compact_rows) - 15} 个低优先级/未到期对象仅保留在 CSV。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def index_handoff_group_messages(
    wechat_cli_value: str,
    relations: list[dict[str, str]],
    since: str | None,
    until: str | None,
    per_group_limit: int,
    db_path: str,
    out_dir: Path,
) -> None:
    wechat_cli = Path(wechat_cli_value).expanduser()
    messages: list[Message] = []
    seen_groups: set[str] = set()
    for relation in relations:
        group_id = relation["共同群ID"]
        if group_id in seen_groups:
            continue
        seen_groups.add(group_id)
        session = {
            "username": group_id,
            "display_name": relation["共同群名"] or group_id,
            "chat_type": "group",
        }
        messages.extend(timeline_messages_for_group(wechat_cli, session, since, until, per_group_limit))
    messages = sorted(dedupe_messages(messages), key=lambda message: message.time or "")
    if messages:
        store_messages_to_db(db_path, "handoff_groups", since, until, messages, out_dir)


def reactivation(args: argparse.Namespace) -> None:
    labels = args.label or configured_labels("reactivation")
    self_names = configured_self_names(args.self_name)
    contacts_path = Path(args.contacts).expanduser()
    targets = load_label_contact_rows(contacts_path, labels)
    handoff_relations = load_handoff_relations(args.handoffs)
    if not targets:
        raise SystemExit(f"没有找到标签联系人：{contacts_path} / {'、'.join(labels)}")

    if args.index_first:
        db_index(
            argparse.Namespace(
                wechat_cli=args.wechat_cli,
                scope="labels",
                contacts=str(contacts_path),
                label=labels,
                target_limit=args.target_limit,
                session_type="all",
                session_limit=80,
                per_chat_limit=args.per_chat_limit,
                keyword=None,
                chat=None,
                search_limit=100,
                max_pages=3,
                max_text_chars=1000,
                since=args.since,
                until=args.until,
                default_24h=False,
                exclude_list=args.exclude_list,
                db=args.db,
                out=str(Path(args.out).expanduser() / "_index"),
            )
        )
        if handoff_relations:
            index_handoff_group_messages(
                args.wechat_cli,
                handoff_relations,
                args.since,
                args.until,
                args.per_chat_limit,
                args.db,
                Path(args.out).expanduser() / "_handoff_groups",
            )

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}。可以先加 --index-first。")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_radar_db(conn)
    rows = build_reactivation_rows(
        targets[: args.target_limit],
        conn,
        args.since,
        args.per_chat_limit,
        args.inactive_days,
        self_names,
        handoff_relations,
    )
    conn.close()

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "复联类型",
        "分数",
        "联系人",
        "标签",
        "最后聊天时间",
        "最后一条方向",
        "沉默天数",
        "建议跟进日期",
        "跟进状态",
        "消息数",
        "商单信号数",
        "阶段分布",
        "报价/佣金/预算",
        "交接关系",
        "建议动作",
        "复联开场建议",
        "证据摘要",
    ]
    write_csv(out_dir / "reactivation_candidates.csv", rows, fieldnames)
    write_reactivation_report(out_dir / "reactivation_report.md", rows, labels, args.since, args.inactive_days)
    counts = Counter(row["复联类型"] for row in rows)
    print(f"标签范围：{'、'.join(labels)}")
    print(f"覆盖联系人：{len(rows)} 个")
    print(f"优先复联：{counts.get('优先复联', 0)} 个")
    print(f"复购保温：{counts.get('复购保温', 0)} 个")
    print(f"可复联：{counts.get('可复联', 0)} 个")
    print(f"待交接跟进：{counts.get('待交接跟进', 0)} 个")
    print(f"待下一批跟进：{counts.get('待下一批跟进', 0)} 个")
    print(f"失败可修复：{counts.get('失败可修复', 0)} 个")
    print(f"已交接：{counts.get('已交接', 0)} 个")
    print(f"我方主动放弃：{counts.get('我方主动放弃', 0)} 个")
    print(f"纯佣低优先级：{counts.get('纯佣低优先级', 0)} 个")
    print(f"输出文件：{out_dir / 'reactivation_report.md'}")


def messages_to_markdown(title: str, messages: list[Message], *, query: str | None = None) -> str:
    lines = [f"# {title}", ""]
    if query:
        lines.append(f"- 关键词：{query}")
    lines.append(f"- 消息数：{len(messages)}")
    if messages:
        times = sorted(message.time for message in messages if message.time)
        if times:
            lines.append(f"- 时间范围：{times[0]} 至 {times[-1]}")
    lines.append("")
    for message in messages:
        lines.append(f"- **{message.time}｜{message.chat}｜{message.sender}**：{shorten(message.content, 320)}")
    return "\n".join(lines) + "\n"


def write_message_outputs(out_dir: Path, markdown_name: str, messages: list[Message], markdown: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / markdown_name).write_text(markdown, encoding="utf-8")
    (out_dir / "messages.json").write_text(json.dumps([asdict(message) for message in messages], ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "messages.jsonl").write_text(
        "\n".join(json.dumps(asdict(message), ensure_ascii=False) for message in messages) + ("\n" if messages else ""),
        encoding="utf-8",
    )


def chat_history(args: argparse.Namespace) -> None:
    wechat_cli = require_compatible_wechat_cli(args.wechat_cli)
    session = resolve_chat_session(wechat_cli, args.chat, args.type_filter)
    messages = timeline_messages_for_group(wechat_cli, session, args.since, args.until, args.limit)
    if args.query:
        needle = args.query.lower()
        messages = [message for message in messages if needle in message.content.lower() or needle in message.sender.lower()]
    messages = sorted(dedupe_messages(messages), key=lambda message: message.time or "")
    out_dir = Path(args.out).expanduser()
    title = f"微信聊天记录：{session['display_name']}"
    markdown = messages_to_markdown(title, messages, query=args.query)
    write_message_outputs(out_dir, "chat_history.md", messages, markdown)
    print(f"聊天对象：{session['display_name']}")
    print(f"消息：{len(messages)} 条")
    print(f"输出文件：{out_dir / 'chat_history.md'}")


def common_groups(args: argparse.Namespace) -> None:
    wechat_cli = require_compatible_wechat_cli(args.wechat_cli)
    resolved_contacts = [resolve_chat_session(wechat_cli, contact, "private") for contact in args.contact]
    target_usernames = {contact["username"] for contact in resolved_contacts}
    sessions = fetch_group_sessions(wechat_cli, args.group_limit, args.since)
    matches: list[dict[str, Any]] = []
    all_messages: list[Message] = []

    def inspect_session(session: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return session, fetch_group_members(wechat_cli, session, args.member_limit)

    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as pool:
        inspected_sessions = pool.map(inspect_session, sessions)

    for session, members in inspected_sessions:
        member_usernames = {str(member.get("username") or "") for member in members}
        if not target_usernames.issubset(member_usernames):
            continue
        selected_members = [
            {
                "username": str(member.get("username") or ""),
                "display_name": str(member.get("display_name") or member.get("nick_name") or member.get("remark") or ""),
            }
            for member in members
            if str(member.get("username") or "") in target_usernames
        ]
        messages = timeline_messages_for_group(wechat_cli, session, args.since, args.until, args.per_group_limit)
        messages = filter_by_time(messages, args.since, args.until)
        all_messages.extend(messages)
        matches.append(
            {
                "chatroom_id": str(session.get("username") or ""),
                "display_name": str(session.get("display_name") or session.get("username") or ""),
                "last_timestamp": session.get("last_timestamp"),
                "members": selected_members,
                "messages": [asdict(message) for message in messages],
            }
        )

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    contact_names = "、".join(contact["display_name"] for contact in resolved_contacts)
    lines = [
        "# 微信共同群",
        "",
        f"- 联系人：{contact_names}",
        f"- 扫描群聊：{len(sessions)} 个",
        f"- 命中共同群：{len(matches)} 个",
        f"- 时间范围：{args.since or '不限'} 至 {args.until or '不限'}",
        "",
    ]
    if not matches:
        lines.append("没有找到同时包含这些联系人的群聊。")
    for match in matches:
        lines.extend(
            [
                f"## {match['display_name']}",
                "",
                f"- 群 ID：{match['chatroom_id']}",
                f"- 匹配成员：{'、'.join(member['display_name'] for member in match['members'])}",
                f"- 时间范围内消息：{len(match['messages'])} 条",
                "",
            ]
        )
        for message in match["messages"][-30:]:
            lines.append(
                f"- **{message['time']}｜{message['sender']}**：{shorten(str(message['content']), 240)}"
            )
        lines.append("")

    report_path = out_dir / "common_groups.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "common_groups.json").write_text(
        json.dumps(
            {
                "contacts": resolved_contacts,
                "groups_scanned": len(sessions),
                "matches": matches,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if not args.no_db and all_messages:
        store_messages_to_db(args.db, "common_groups", args.since, args.until, all_messages, out_dir)
    print(f"联系人：{contact_names}")
    print(f"扫描群聊：{len(sessions)} 个")
    print(f"命中共同群：{len(matches)} 个")
    print(f"索引群消息：{len(all_messages)} 条")
    print(f"输出文件：{report_path}")


def chat_search(args: argparse.Namespace) -> None:
    wechat_cli = require_compatible_wechat_cli(args.wechat_cli)
    messages: list[Message] = []
    raw_payloads: list[dict[str, Any]] = []
    offset = 0
    for _ in range(args.max_pages):
        command = [
            str(wechat_cli),
            "search",
            args.query,
            "--limit",
            str(args.limit),
            "--offset",
            str(offset),
            "--max-text-chars",
            str(args.max_text_chars),
        ]
        if args.chat:
            command.extend(["--in", args.chat])
        if args.since:
            command.extend(["--after", args.since])
        if args.until:
            command.extend(["--before", args.until])
        raw = run_json_command(command)
        raw_payloads.append({"query": args.query, "offset": offset, "raw": raw})
        page_messages = rows_to_messages(extract_json_rows(raw), f"wechat-cli:chat-search:{args.query}:offset:{offset}")
        messages.extend(page_messages)
        query_meta = extract_query(raw)
        if not query_meta.get("has_more") or not page_messages:
            break
        next_offset = query_meta.get("next_offset")
        if isinstance(next_offset, int) and next_offset > offset:
            offset = next_offset
        else:
            offset += len(page_messages)

    messages = dedupe_messages(messages)
    messages = filter_by_time(messages, args.since, args.until)
    messages = filter_by_exclude_list(messages, load_watchlist(args.exclude_list))
    messages = sorted(messages, key=lambda message: message.time or "", reverse=True)
    out_dir = Path(args.out).expanduser()
    markdown = messages_to_markdown(f"微信全局聊天搜索：{args.query}", messages, query=args.query)
    write_message_outputs(out_dir, "search_results.md", messages, markdown)
    (out_dir / "search_raw.json").write_text(json.dumps(raw_payloads, ensure_ascii=False, indent=2), encoding="utf-8")
    inserted_messages = inserted_links = 0
    db_file: Path | None = None
    if not args.no_db:
        db_file, inserted_messages, inserted_links = store_messages_to_db(args.db, "chat_search", args.since or "", args.until or "", messages, out_dir)
    print(f"关键词：{args.query}")
    print(f"命中：{len(messages)} 条")
    if db_file:
        print(f"新写入消息：{inserted_messages} 条")
        print(f"新写入链接：{inserted_links} 条")
        print(f"本地情报库：{db_file}")
    print(f"输出文件：{out_dir / 'search_results.md'}")


def db_status(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    conn = connect_readonly_radar_db(str(db_path))
    counts = {
        "runs": conn.execute("select count(*) from runs").fetchone()[0],
        "messages": conn.execute("select count(*) from messages").fetchone()[0],
        "group_summaries": conn.execute("select count(*) from group_summaries").fetchone()[0],
        "message_links": conn.execute("select count(*) from message_links").fetchone()[0],
        "opportunities": conn.execute("select count(*) from opportunities").fetchone()[0],
        "candidates": conn.execute(
            "select count(*) from opportunities where record_type = 'candidate' and status = 'new'"
        ).fetchone()[0],
        "formal_opportunities": conn.execute(
            "select count(*) from opportunities where record_type = 'opportunity'"
        ).fetchone()[0],
        "stale_candidates": conn.execute(
            "select count(*) from opportunities where record_type = 'candidate' and status = 'stale'"
        ).fetchone()[0],
        "open_opportunities": conn.execute(
            "select count(*) from opportunities where status in ('new', 'active', 'waiting', 'paused')"
        ).fetchone()[0],
        "feedback": conn.execute("select count(*) from opportunity_feedback").fetchone()[0],
    }
    latest = conn.execute(
        "select run_type, since_time, until_time, created_at, groups_count, messages_count, output_dir from runs order by id desc limit 1"
    ).fetchone()
    conn.close()
    print(f"数据库：{db_path}")
    print(f"运行记录：{counts['runs']} 次")
    print(f"消息：{counts['messages']} 条")
    print(f"群摘要：{counts['group_summaries']} 条")
    print(f"链接：{counts['message_links']} 条")
    print(f"商机：{counts['opportunities']} 个（开放 {counts['open_opportunities']} 个）")
    print(
        f"  待审核候选：{counts['candidates']} 个；正式机会：{counts['formal_opportunities']} 个；"
        f"过期候选：{counts['stale_candidates']} 个"
    )
    print(f"人工反馈：{counts['feedback']} 条")
    if latest:
        print("最近一次：")
        print(f"  类型：{latest['run_type']}")
        print(f"  时间范围：{latest['since_time']} 至 {latest['until_time']}")
        print(f"  群聊：{latest['groups_count']} 个")
        print(f"  消息：{latest['messages_count']} 条")
        print(f"  输出：{latest['output_dir']}")


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def cleanup_command(args: argparse.Namespace) -> None:
    try:
        candidates = find_cleanup_candidates(
            Path(args.root),
            raw_days=args.raw_days,
            report_days=args.report_days,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    raw_count = sum(item.kind == "raw" for item in candidates)
    report_count = len(candidates) - raw_count
    total_size = sum(item.size for item in candidates)
    mode = "执行清理" if args.apply else "清理预览"
    print(f"{mode}：{len(candidates)} 个文件，可释放 {human_size(total_size)}")
    print(f"  原始/结构化文件：{raw_count} 个（保留 {args.raw_days} 天）")
    print(f"  报告文件：{report_count} 个（保留 {args.report_days} 天）")
    for item in candidates[:50]:
        print(f"- [{item.kind}] {item.age_days} 天｜{human_size(item.size)}｜{item.path}")
    if len(candidates) > 50:
        print(f"... 其余 {len(candidates) - 50} 个文件未展开")

    if not args.apply:
        print("当前仅预览，没有删除文件。确认后加 --apply。")
        return
    removed, reclaimed = apply_cleanup(candidates)
    print(f"已删除：{removed} 个文件，释放 {human_size(reclaimed)}")


OPPORTUNITY_STATUS_LABELS = {
    "new": "新发现",
    "active": "推进中",
    "waiting": "等待对方",
    "paused": "暂缓",
    "won": "已成交",
    "lost": "未成交",
    "ignored": "已忽略",
    "stale": "已过期",
    "archived": "已归档",
}

OPPORTUNITY_RECORD_LABELS = {
    "candidate": "待审核候选",
    "opportunity": "正式机会",
}

OPPORTUNITY_CONFIDENCE_LABELS = {
    "low": "低置信",
    "medium": "待核实",
    "high": "高置信",
    "confirmed": "人工确认",
}


def opportunity_rows_to_markdown(rows: list[dict[str, Any]], title: str = "微信商机管线") -> str:
    lines = [f"# {title}", ""]
    if not rows:
        lines.append("暂无符合条件的商机。")
        return "\n".join(lines) + "\n"
    for row in rows:
        follow_up = f"，跟进：{row['next_follow_up']}" if row["next_follow_up"] else ""
        if not follow_up and row["status"] == "waiting":
            follow_up = "，⚠️ 等待中但没有跟进日期，请补一个"
        amount = f"，预算/报价：{row['amount']}" if row["amount"] else ""
        display_title = row["title"] or row["chat"]
        record_label = OPPORTUNITY_RECORD_LABELS.get(
            str(row.get("record_type") or "candidate"), "待审核候选"
        )
        confidence_label = OPPORTUNITY_CONFIDENCE_LABELS.get(
            str(row.get("confidence") or "medium"), "待核实"
        )
        lines.append(
            f"- **#{row['id']}｜{display_title}**：{OPPORTUNITY_STATUS_LABELS.get(row['status'], row['status'])}"
            f" / {record_label} / {confidence_label} / {row['stage']} / {row['opportunity_type']}，"
            f"优先级 {row['priority']}{amount}{follow_up}"
        )
        lines.append(f"  - 下一步：{row['next_action'] or '待人工确认'}")
        lines.append(
            f"  - 最后信号：{row['last_signal_time'] or '未知'}；证据 {row['evidence_count']} 条；"
            f"强化 {row.get('reinforcement_count') or 1} 次"
        )
        if row.get("qualification_reasons"):
            lines.append(f"  - 晋级依据：{row['qualification_reasons']}")
        if row.get("record_type") == "candidate" and row.get("expires_at"):
            lines.append(f"  - 候选过期：{row['expires_at']}")
        if row["notes"]:
            lines.append(f"  - 备注：{shorten(str(row['notes']).splitlines()[-1], 160)}")
    return "\n".join(lines) + "\n"


def write_or_print_report(text: str, out_value: str | None, item_label: str, count: int) -> None:
    if out_value:
        out_path = Path(out_value).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"{item_label}：{count} 个")
        print(f"输出文件：{out_path}")
    else:
        print(text)


def person_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    person_query = args.person
    refresh_summary = ""
    if args.refresh:
        wechat_cli = require_compatible_wechat_cli(args.wechat_cli)
        session = resolve_chat_session(wechat_cli, args.person, args.type_filter)
        messages = timeline_messages_for_group(
            wechat_cli,
            session,
            args.since,
            args.until,
            args.limit,
        )
        _, inserted_messages, _ = store_messages_to_db(
            args.db,
            "person_refresh",
            args.since,
            args.until,
            messages,
            Path("output/person-refresh"),
        )
        person_query = str(session.get("display_name") or args.person)
        refresh_summary = f"已实时刷新 {len(messages)} 条，新增索引 {inserted_messages} 条。"
    conn = connect_readonly_radar_db(str(db_path))
    try:
        text, metadata = person_report(
            conn,
            person_query,
            since=args.since,
            limit=args.limit,
            self_names=configured_self_names(args.self_name),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        conn.close()
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"联系人：{metadata['chat']}")
        if refresh_summary:
            print(refresh_summary)
        print(f"分析消息：{metadata['messages']} 条；开放商机：{metadata['opportunities']} 个")
        print(f"输出文件：{out_path}")
    else:
        if refresh_summary:
            print(refresh_summary)
        print(text)


def home_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    conn = connect_readonly_radar_db(str(db_path))
    text, metadata = home_report(conn)
    conn.close()
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(
            f"本地消息：{metadata['messages']} 条；开放商机：{metadata['open_opportunities']} 个；"
            f"今日到期：{metadata['due_opportunities']} 个"
        )
        print(f"输出文件：{out_path}")
    else:
        print(text)


def reply_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    conn = connect_readonly_radar_db(str(db_path))
    try:
        text, metadata = reply_report(
            conn,
            args.person,
            limit=args.limit,
            self_names=configured_self_names(args.self_name),
            style_days=args.style_days or configured_reply_style_int("history_days", 30),
            minimum_chat_messages=(
                args.minimum_chat_messages
                or configured_reply_style_int("minimum_chat_messages", 5)
            ),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        conn.close()
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"联系人：{metadata['chat']}；识别场景：{metadata['intent']}")
        print(f"输出文件：{out_path}")
    else:
        print(text)


def topic_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    since = args.since
    if not since and args.days:
        since = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = connect_readonly_radar_db(str(db_path))
    text, metadata = topic_report(
        conn,
        args.topic,
        extra_keywords=args.keyword or [],
        since=since,
        limit_messages=args.limit_messages,
        limit_chats=args.limit_chats,
    )
    conn.close()
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"主题：{args.topic}；命中：{metadata['messages']} 条；会话：{metadata['chats']} 个")
        print(f"输出文件：{out_path}")
    else:
        print(text)


def brief_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    conn = connect_readonly_radar_db(str(db_path))
    text, metadata = brief_report(
        conn,
        hours=args.hours,
        limit_chats=args.limit_chats,
        self_names=configured_self_names(args.self_name),
    )
    conn.close()
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(
            f"近 {args.hours} 小时：{metadata['messages']} 条消息，{metadata['chats']} 个会话，"
            f"{metadata['opportunities']} 个新近商机"
        )
        print(f"输出文件：{out_path}")
    else:
        print(text)


CONTACT_ACK_TERMS = re.compile(
    r"^(?:(?:好(?:的|呀|滴)?|收到|明白|嗯嗯|没问题|可以|谢谢(?:老师)?|辛苦(?:老师)?了?|不客气)"
    r"[呀啊哈啦~～，,、。！!\s]*)+$", re.I,
)
CONTACT_REPLY_REQUEST_TERMS = re.compile(
    r"请问|麻烦|方便|能否|可以吗|怎么|多少|什么时候|哪天|确认一下|回复一下|发我|给我|"
    r"报价|预算|费用|价格|brief|排期|初稿|二稿|终稿|审核|修改|结算|付款|发票|invoice|payment|\?|？",
    re.I,
)


def contact_reply_direction(content: str, stage: str = "") -> str:
    text = f"{content} {stage}"
    if re.search(r"结算|付款|打款|发票|invoice|payment", text, re.I):
        return "先核对发布与结算材料，直接回复缺什么、何时可以补齐。"
    if re.search(r"审核|修改|反馈|初稿|二稿|终稿", text, re.I):
        return "直接确认修改范围和下一版时间，不重复介绍背景。"
    if re.search(r"报价|预算|费用|价格|多少", text, re.I):
        return "先回答报价问题；只补问缺失的交付形式、授权范围和排期。"
    if re.search(r"brief|需求|素材|卖点", text, re.I):
        return "确认已收到需求，集中列出待确认项并给出下一节点。"
    if re.search(r"排期|时间|什么时候|哪天|发布", text, re.I):
        return "给明确可执行日期；暂时不能确认时，说明最晚何时回准信。"
    if CONTACT_REPLY_REQUEST_TERMS.search(content):
        return "先直接回答对方最后一个问题，再补一句明确的下一步。"
    return "承接对方最新内容并推进到下一节点；没有新信息时不要为了回复而回复。"


def contact_daily_status(
    latest: Message,
    self_names: list[str],
    has_commercial_context: bool,
) -> tuple[str, str]:
    if is_self_sender(latest.sender, self_names):
        return "等待对方", "暂不追发；若超过约定时间仍无回复，再按商机状态跟进。"
    if CONTACT_ACK_TERMS.search(latest.content.strip()):
        return "无需立即回复", "对方只是确认收到；继续完成已经承诺的动作即可。"
    if CONTACT_REPLY_REQUEST_TERMS.search(latest.content) or has_commercial_context:
        return "待回复", contact_reply_direction(latest.content)
    return "留意", "先看前后文判断是否需要承接；没有明确问题或动作时可不回复。"


def find_open_contact_promises(
    conn: sqlite3.Connection,
    chat: str,
    until: str,
    self_names: list[str],
    *,
    lookback_days: int = 30,
    chat_id: str | None = None,
) -> list[dict[str, str]]:
    register_time_sql(conn)
    until_dt = parse_time_for_filter(until) or datetime.now()
    history_since = (until_dt - timedelta(days=max(1, lookback_days))).strftime("%Y-%m-%d %H:%M:%S")
    history = [
        dict(row)
        for row in conn.execute(
            """
            select chat, sender, time, content, source_file
            from messages
            where chat = ? and message_epoch(time) >= message_epoch(?) and message_epoch(time) <= message_epoch(?)
            order by message_epoch(time) asc, id asc
            """,
            (chat, history_since, until),
        ).fetchall()
    ]
    if chat_id is not None:
        history = [row for row in history if source_chat_id(str(row["source_file"])) == chat_id]
    return [{
        "时间": str(promise["time"]),
        "内容": shorten(str(promise["content"]), 220),
        "下一步": infer_promise_action(str(promise["content"])),
    } for promise in open_promise_rows(history, self_names)]


def find_open_contact_promise(
    conn: sqlite3.Connection, chat: str, until: str, self_names: list[str], *, lookback_days: int = 30,
) -> dict[str, str] | None:
    """Legacy single-item API; new reports use the complete promise list."""
    promises = find_open_contact_promises(conn, chat, until, self_names, lookback_days=lookback_days)
    return promises[-1] if promises else None


def build_contact_daily_rows(
    conn: sqlite3.Connection,
    since: str,
    until: str,
    label_index: dict[str, set[str]],
    self_names: list[str],
) -> list[dict[str, Any]]:
    register_time_sql(conn)
    validate_window(since, until)
    db_rows = conn.execute(
        """
        select chat, sender, time, content, source_file
        from messages
        where message_epoch(time) >= message_epoch(?) and message_epoch(time) <= message_epoch(?)
        order by message_epoch(time) asc, id asc
        """,
        (since, until),
    ).fetchall()
    groups = load_known_group_chats(conn)
    by_chat: dict[tuple[str, str], list[Message]] = {}
    for row in db_rows:
        message = Message(**dict(row))
        if message.chat in groups or "@chatroom" in message.source_file:
            continue
        by_chat.setdefault(conversation_key(message.chat, message.source_file), []).append(message)

    open_opportunities: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        """
        select id, chat, title, opportunity_type, stage, status, priority,
               next_action, next_follow_up, last_signal_time,
               stage_locked, priority_locked, next_action_locked
        from opportunities
        where status in ('new', 'active', 'waiting', 'paused')
        order by priority desc, last_signal_time desc
        """
    ).fetchall():
        open_opportunities.setdefault(str(row["chat"]), []).append(dict(row))
    identities_by_name: dict[str, set[str]] = {}
    for row in conn.execute("select distinct chat, source_file from messages"):
        identities_by_name.setdefault(str(row["chat"]), set()).add(source_chat_id(str(row["source_file"])))
    rows: list[dict[str, Any]] = []
    priority_labels = set(configured_labels("priority"))
    commercial_labels = set(configured_labels("commercial"))
    creator_labels = set(configured_labels("creator"))
    contact_daily_config = ACTIVE_PROFILE.get("contact_daily")
    contact_scope = (
        str(contact_daily_config.get("scope") or "hybrid")
        if isinstance(contact_daily_config, dict)
        else "hybrid"
    )
    personal_patterns = {
        label: pattern
        for label, pattern in configured_group_value_patterns().items()
        if label not in GROUP_VALUE_PATTERNS
    }
    for (chat, chat_id), messages in by_chat.items():
        messages = sorted(dedupe_messages(messages), key=lambda message: timestamp_epoch(message.time) or 0)
        if chat == "服务通知" or all(
            "notifymessage" in message.source_file or message.sender.endswith("@app")
            for message in messages
        ):
            continue
        labels = set(matched_contact_labels(chat, "", label_index))
        has_self_message = any(is_self_sender(message.sender, self_names) for message in messages)
        commercial_messages = [
            message for message in messages
            if DIRECT_DEAL_TERMS.search(message.content)
            or TRAINING_PROJECT_TERMS.search(message.content)
            or MONETIZATION_TERMS.search(message.content)
        ]
        external_commercial_messages = [
            message for message in commercial_messages
            if not is_self_sender(message.sender, self_names)
        ]
        personal_priority_messages = [
            message for message in messages
            if any(pattern.search(message.content or "") for pattern in personal_patterns.values())
        ]
        external_personal_priority_messages = [
            message for message in personal_priority_messages
            if not is_self_sender(message.sender, self_names)
        ]
        opportunities = open_opportunities.get(chat, [])
        identity_ambiguous = len(identities_by_name.get(chat, set()) - {""}) > 1
        # Legacy opportunities are name-keyed; do not attribute them to either
        # of two same-named people without an explicit identity binding.
        if identity_ambiguous:
            opportunities = []
        opportunity = opportunities[0] if opportunities else None
        is_priority_contact = bool(labels & (priority_labels | commercial_labels | creator_labels))
        if contact_scope == "priority_labels_only" and not is_priority_contact:
            continue
        manually_tracked = any(
            item.get("status") in {"active", "waiting", "paused"}
            or item.get("stage_locked") or item.get("priority_locked") or item.get("next_action_locked")
            for item in opportunities
        )
        has_two_way_commercial_context = bool(has_self_message and external_commercial_messages)
        first_inbound_request = any(
            CONTACT_REPLY_REQUEST_TERMS.search(message.content)
            or re.search(r"请问|怎么收费|多少钱|需要.{0,20}(?:培训|讲师|推广|定制)|(?:合作|商单).{0,16}(?:推荐你|找你|邀请你)", message.content, re.I)
            for message in external_commercial_messages
        )
        has_two_way_personal_context = bool(has_self_message and external_personal_priority_messages)
        if not (
            is_priority_contact
            or manually_tracked
            or first_inbound_request
            or has_two_way_commercial_context
            or has_two_way_personal_context
        ):
            continue
        if labels & commercial_labels and labels & creator_labels:
            role = "客户/品牌 + 同行/创作者"
        elif labels & commercial_labels:
            role = "客户/品牌/商务联系人"
        elif labels & creator_labels:
            role = "同行/创作者/资源方"
        elif labels & priority_labels:
            role = "重点标签联系人（" + " / ".join(sorted(labels & priority_labels)) + "）"
        elif has_two_way_personal_context:
            role = "个人重点主题联系人"
        else:
            role = "新商业联系人"
        latest = messages[-1]
        stage = str(opportunity.get("stage") or "") if opportunity else ""
        status, reply_direction = contact_daily_status(
            latest,
            self_names,
            bool(commercial_messages or personal_priority_messages or opportunity),
        )
        promises = find_open_contact_promises(conn, chat, until, self_names, chat_id=chat_id)
        open_promise = promises[-1] if promises else None
        if open_promise:
            status = "待兑现"
            reply_direction = "；".join(dict.fromkeys(promise["下一步"] for promise in promises)) + "；按下方承诺逐项核实，不要把一个项目的完成当作全部完成。"
        rows.append(
            {
                "联系人": chat,
                "会话ID": chat_id,
                "身份待核实": identity_ambiguous,
                "角色": role,
                "标签": sorted(labels),
                "消息数": len(messages),
                "最后时间": latest.time,
                "最后发言人": latest.sender,
                "最后消息": shorten(latest.content, 220),
                "状态": status,
                "回复建议": reply_direction,
                "历史承诺": open_promise,
                "待兑现承诺": promises,
                "商业消息数": len(commercial_messages),
                "个人重点消息数": len(personal_priority_messages),
                "商机": opportunity,
                "商机列表": opportunities,
                "最近消息": [asdict(message) for message in messages[-6:]],
            }
        )
    status_rank = {"待兑现": 5, "待回复": 4, "等待对方": 3, "留意": 2, "无需立即回复": 1}
    rows.sort(
        key=lambda row: (
            status_rank.get(str(row["状态"]), 0),
            int((row.get("商机") or {}).get("priority") or 0),
            int(row["商业消息数"]),
            int(row["个人重点消息数"]),
            str(row["最后时间"]),
        ),
        reverse=True,
    )
    return rows


def write_contact_daily_report(path: Path, rows: list[dict[str, Any]], since: str, until: str) -> None:
    counts = Counter(str(row["状态"]) for row in rows)
    contact_daily_config = ACTIVE_PROFILE.get("contact_daily")
    contact_scope = (
        str(contact_daily_config.get("scope") or "hybrid")
        if isinstance(contact_daily_config, dict)
        else "hybrid"
    )
    priority_label_names = configured_labels("priority")
    lines = [
        "# 重点联系人私聊日报",
        "",
        f"> {since} 至 {until}｜覆盖 {len(rows)} 个重点联系人",
        "",
        "## 一眼结论",
        "",
        f"- 待兑现：**{counts['待兑现']}** 个",
        f"- 待回复：**{counts['待回复']}** 个",
        f"- 等待对方：**{counts['等待对方']}** 个",
        f"- 留意：**{counts['留意']}** 个",
        f"- 无需立即回复：**{counts['无需立即回复']}** 个",
        "",
        "## 先处理这些",
        "",
    ]
    pending = [row for row in rows if row["状态"] in {"待兑现", "待回复"}]
    if not pending:
        lines.append("当前窗口没有检出待兑现承诺或明确待回复的重点联系人。")
    for row in pending[:12]:
        opportunity = row.get("商机") or {}
        stage = f"；商机阶段：{opportunity.get('stage')}" if opportunity else ""
        lines.append(
            f"- **{row['联系人']}｜{row['角色']}**：{row['最后消息']}{stage} "
            f"回复方向：{row['回复建议']}"
        )

    lines.extend(["", "## 按联系人查看", "", "> 点击联系人展开最近消息和当前商机。", ""])
    for row in rows:
        opportunity = row.get("商机") or {}
        lines.extend(
            [
                "<details>",
                f"<summary><strong>{row['联系人']}</strong>｜{row['角色']}｜{row['状态']}｜{row['消息数']} 条</summary>",
                "",
                f"- **最后消息**：{row['最后时间']}｜{row['最后发言人']}：{row['最后消息']}",
                f"- **回复建议**：{row['回复建议']}",
            ]
        )
        if row.get("身份待核实"):
            lines.append("- **身份待核实**：发现同名不同会话；旧商机尚未绑定会话 ID，请先核对，未自动归入此人。")
        for opportunity in row.get("商机列表", [opportunity] if opportunity else []):
            lines.append(
                f"- **当前商机**：#{opportunity.get('id')}｜{opportunity.get('title')}｜{opportunity.get('stage')}｜"
                f"{opportunity.get('next_action') or '待人工确认'}｜跟进：{opportunity.get('next_follow_up') or '待确认'}"
            )
        for promise in row.get("待兑现承诺", [row["历史承诺"]] if row.get("历史承诺") else []):
            lines.append(
                f"- **未兑现承诺**：{promise.get('时间')}｜{promise.get('内容')}｜{promise.get('下一步')}"
            )
        lines.extend(["", "### 最近消息", ""])
        for message in row["最近消息"]:
            lines.append(
                f"- **{message.get('time', '')}｜{message.get('sender', '')}**："
                f"{shorten(str(message.get('content') or ''), 220)}"
            )
        lines.extend(["", "</details>", ""])
    lines.extend(
        [
            "## 说明",
            "",
            (
                "- 本报告仅覆盖 Profile 重点标签："
                + ("、".join(priority_label_names) or "尚未设置")
                + "。未打这些标签的联系人不进入重点联系人页。"
                if contact_scope == "priority_labels_only"
                else "- 本报告覆盖个人 Profile 重点标签、开放商机、首次入站商业线索、双向商业对话，或命中个人自定义重点主题的私聊。入站线索不等于已确认合作。"
            ),
            "- 微信标签不自动等同于真实角色；具体身份仍需查看原始聊天核实。",
            "- 回复建议是方向，不会发送微信；金额、排期和承诺必须人工确认。",
        ]
    )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def contact_daily_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    if args.since:
        since = normalize_time(args.since)
        until = normalize_time(args.until) if args.until else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        until_dt = parse_time_for_filter(args.until) if args.until else datetime.now()
        until_dt = until_dt or datetime.now()
        since = (until_dt - timedelta(hours=max(1, args.hours))).strftime("%Y-%m-%d %H:%M:%S")
        until = until_dt.strftime("%Y-%m-%d %H:%M:%S")
    conn = connect_readonly_radar_db(str(db_path))
    label_index = load_contact_label_index(args.contacts)
    rows = build_contact_daily_rows(
        conn,
        since,
        until,
        label_index,
        configured_self_names(args.self_name),
    )[: max(1, args.limit)]
    conn.close()
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    write_contact_daily_report(out_dir / "contact_daily_digest.md", rows, since, until)
    (out_dir / "contact_daily.json").write_text(
        json.dumps({"since": since, "until": until, "contacts": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"时间范围：{since} 至 {until}")
    print(f"重点联系人：{len(rows)} 个；待回复：{sum(row['状态'] == '待回复' for row in rows)} 个")
    print(f"输出文件：{out_dir / 'contact_daily_digest.md'}")


def today_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    conn = connect_readonly_radar_db(str(db_path))
    rows = list_today_opportunities(
        conn,
        min_priority=args.min_priority,
        limit=args.limit,
    )
    conn.close()
    text = opportunity_rows_to_markdown(rows, title="微信个人情报库｜今日行动")
    if rows:
        text += "\n先处理到期跟进和待结算，再用 `triage <ID> <决定>` 更新结果。\n"
    write_or_print_report(text, args.out, "今日行动", len(rows))


def inbox_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    conn = connect_readonly_radar_db(str(db_path))
    rows = list_opportunity_inbox(
        conn,
        min_priority=args.min_priority,
        limit=args.limit,
    )
    conn.close()
    text = opportunity_rows_to_markdown(rows, title="微信个人情报库｜待分流情报")
    if rows:
        text += (
            "\n分流决定：`pursue` 推进、`wait` 等待、`pause` 暂缓、"
            "`ignore` 忽略、`won` 成交、`lost` 未成交。\n"
        )
    write_or_print_report(text, args.out, "待分流情报", len(rows))


def triage_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    conn = connect_radar_db(str(db_path))
    init_radar_db(conn)
    try:
        with conn:
            row = triage_opportunity(
                conn,
                args.id,
                args.decision,
                next_follow_up=args.follow_up,
                stage=args.stage,
                priority=args.priority,
                next_action=args.next_action,
                note=args.note,
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        conn.close()
    follow_up = f"，下次跟进 {row['next_follow_up']}" if row["next_follow_up"] else ""
    print(
        f"已分流商机 #{row['id']}：{row['chat']} → "
        f"{OPPORTUNITY_STATUS_LABELS.get(row['status'], row['status'])}{follow_up}"
    )


def opportunities_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    conn = connect_readonly_radar_db(str(db_path))
    rows = list_opportunities(
        conn,
        status=args.status,
        stage=args.stage,
        chat=args.chat,
        due_only=args.due_only,
        include_closed=args.include_closed,
        min_priority=args.min_priority,
        record_type=None if args.include_candidates else "opportunity",
        limit=args.limit,
    )
    conn.close()
    text = opportunity_rows_to_markdown(rows)
    write_or_print_report(text, args.out, "商机", len(rows))


def opportunity_maintain_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    conn = connect_radar_db(str(db_path))
    init_radar_db(conn)
    with conn:
        rows = expire_stale_candidates(
            conn,
            stale_days=args.stale_days,
            apply=args.apply,
        )
    conn.close()
    mode = "已标记" if args.apply else "预览"
    print(f"{mode}过期候选：{len(rows)} 个（无新信号超过 {args.stale_days} 天）")
    for row in rows[:30]:
        print(f"- #{row['id']}｜{row['title'] or row['chat']}｜最后信号 {row['last_signal_time']}")
    if not args.apply:
        print("当前没有修改数据库；确认后加 --apply。")


def opportunity_sync_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    conn = connect_radar_db(str(db_path))
    init_radar_db(conn)
    sql = "select chat, sender, time, content, source_file from messages where 1 = 1"
    params: list[Any] = []
    if args.since:
        sql += " and time >= ?"
        params.append(normalize_time(args.since))
    if args.until:
        sql += " and time <= ?"
        params.append(normalize_time(args.until))
    if args.chat:
        sql += " and chat like ?"
        params.append(f"%{args.chat}%")
    sql += " order by time asc"
    messages = [Message(**dict(row)) for row in conn.execute(sql, params).fetchall()]
    messages = filter_by_exclude_list(messages, load_watchlist(args.exclude_list))
    candidates = build_opportunity_candidates(messages, load_known_group_chats(conn))
    if args.dry_run:
        conn.close()
        print(f"读取历史消息：{len(messages)} 条")
        print(f"候选商机：{len(candidates)} 个")
        for candidate in sorted(
            candidates,
            key=lambda row: (int(row["priority"]), str(row["last_signal_time"])),
            reverse=True,
        )[:20]:
            print(
                f"- {candidate['title']}：{candidate['opportunity_type']} / {candidate['stage']} / "
                f"优先级 {candidate['priority']} / {candidate['last_signal_time']}"
            )
        return

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        cursor = conn.execute(
            """
            insert into runs(run_type, since_time, until_time, created_at, groups_count, messages_count, output_dir)
            values (?, ?, ?, ?, ?, ?, '')
            """,
            (
                "opportunity_sync",
                args.since or "",
                args.until or "",
                created_at,
                len({message.chat for message in messages}),
                len(messages),
            ),
        )
        run_id = int(cursor.lastrowid)
        created, updated, skipped = sync_opportunity_candidates(
            conn,
            run_id,
            candidates,
            created_at,
        )
    conn.close()
    print(f"读取历史消息：{len(messages)} 条")
    print(f"候选商机：{len(candidates)} 个")
    print(f"新建：{created} 个")
    print(f"更新：{updated} 个")
    print(f"按人工反馈跳过：{skipped} 个")


def opportunity_update_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    if not any(
        [
            args.stage is not None,
            args.status is not None,
            args.priority is not None,
            args.next_action is not None,
            args.follow_up is not None,
            args.note,
            args.clear_follow_up,
            args.unlock_stage,
            args.unlock_priority,
            args.unlock_next_action,
        ]
    ):
        raise SystemExit("至少提供一项要更新的字段")
    conn = connect_radar_db(str(db_path))
    init_radar_db(conn)
    try:
        with conn:
            row = update_opportunity(
                conn,
                args.id,
                stage=args.stage,
                status=args.status,
                priority=args.priority,
                next_action=args.next_action,
                next_follow_up=args.follow_up,
                note=args.note,
                clear_follow_up=args.clear_follow_up,
                unlock_stage=args.unlock_stage,
                unlock_priority=args.unlock_priority,
                unlock_next_action=args.unlock_next_action,
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        conn.close()
    print(
        f"已更新商机 #{row['id']}：{row['chat']}，"
        f"{OPPORTUNITY_STATUS_LABELS.get(row['status'], row['status'])} / {row['stage']}"
    )


def feedback_add_command(args: argparse.Namespace) -> None:
    conn = connect_radar_db(args.db)
    init_radar_db(conn)
    try:
        with conn:
            feedback_id = add_feedback(
                conn,
                args.target_type,
                args.target,
                args.verdict,
                args.note or "",
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        conn.close()
    print(f"已记录反馈 #{feedback_id}：{args.target_type}={args.target} → {args.verdict}")


def feedback_list_command(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    conn = connect_radar_db(str(db_path))
    init_radar_db(conn)
    rows = list_feedback(conn, target_type=args.target_type, limit=args.limit)
    conn.close()
    if not rows:
        print("暂无人工反馈。")
        return
    for row in rows:
        note = f"：{row['note']}" if row["note"] else ""
        print(
            f"#{row['id']}｜{row['created_at']}｜{row['target_type']}={row['target_key']}｜"
            f"{row['verdict']}{note}"
        )


def db_links(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"找不到数据库：{db_path}")
    conn = connect_radar_db(str(db_path))
    init_radar_db(conn)
    backfill_message_links(conn)
    sql = """
        select l.url, m.chat, m.sender, m.time, m.content
        from message_links l
        join messages m on m.id = l.message_id
        where 1=1
    """
    params: list[Any] = []
    if args.since:
        sql += " and m.time >= ?"
        params.append(normalize_time(args.since))
    if args.until:
        sql += " and m.time <= ?"
        params.append(normalize_time(args.until))
    if args.chat:
        sql += " and m.chat like ?"
        params.append(f"%{args.chat}%")
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    grouped: dict[str, list[Message]] = {}
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        url = str(row["url"])
        message = Message(
            chat=str(row["chat"]),
            sender=str(row["sender"]),
            time=str(row["time"]),
            content=str(row["content"]),
            source_file="radar.db",
        )
        identity = (url, message.chat, message.sender, message.time, message.content)
        if identity in seen:
            continue
        seen.add(identity)
        grouped.setdefault(url, []).append(message)
    link_rows = build_link_rows(grouped, min_chats=args.min_chats)
    link_rows = link_rows[: args.limit]
    out_path = Path(args.out).expanduser() if args.out else Path("output/db-cross-group-links.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_cross_group_link_report(out_path.parent, link_rows, args.since or "不限", args.until or "不限")
    generated = out_path.parent / "cross_group_links.md"
    if generated != out_path:
        out_path.write_text(generated.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"聚合链接：{len(link_rows)} 个")
    print(f"输出文件：{out_path}")


def group_daily(args: argparse.Namespace) -> None:
    wechat_cli = require_compatible_wechat_cli(args.wechat_cli)
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    since, until = resolve_time_window(args.since, args.until, getattr(args, "hours", 24))
    exclude_list = load_watchlist(args.exclude_list)
    joined_channel_markers = load_watchlist(args.joined_channel_list)
    raw_sessions = fetch_group_sessions(wechat_cli, args.group_limit, since)
    sessions = filter_sessions_by_exclude_list(raw_sessions, exclude_list)
    all_messages: list[Message] = []
    failed_sessions: list[dict[str, str]] = []
    incomplete_sessions: list[dict[str, Any]] = []
    succeeded_sessions = 0
    for session in sessions:
        messages: list[Message] | None = None
        last_error = ""
        for attempt in range(2):
            try:
                messages = timeline_messages_for_group(
                    wechat_cli,
                    session,
                    since,
                    until,
                    args.per_group_limit,
                )
                break
            except SystemExit as exc:
                last_error = sanitized_compat_error(exc)
                if attempt == 0:
                    time.sleep(0.25)
        if messages is None:
            failed_sessions.append(
                {
                    "chat": str(session.get("display_name") or "未命名群聊"),
                    "error": last_error or "timeline read failed",
                }
            )
            continue
        succeeded_sessions += 1
        timeline_coverage = session.get("_timeline_coverage", {})
        if timeline_coverage.get("incomplete"):
            incomplete_sessions.append({"chat": str(session.get("display_name") or "未命名群聊"), **timeline_coverage})
        messages = filter_by_time(messages, since, until)
        if not messages:
            continue
        all_messages.extend(messages)

    all_messages = dedupe_messages(all_messages)
    messages_by_group: dict[str, list[Message]] = {}
    for message in all_messages:
        messages_by_group.setdefault(message.chat, []).append(message)

    summaries: list[dict[str, str]] = []
    for messages in messages_by_group.values():
        summary = summarize_group(messages, joined_channel_markers)
        if summary:
            summaries.append(summary)

    link_rows = aggregate_links(all_messages, min_chats=args.min_link_chats)
    apply_cross_group_deal_signals(summaries, link_rows)
    summaries.sort(
        key=lambda row: (
            int(row["培训/项目合作机会数"]),
            int(row["重要信号数"]),
            int(row["跨群高概率商单"]),
            int(row["跨群疑似商单"]),
            int(row["商单机会数"]),
            int(row["变现机会数"]),
            int(row["商单信号数"]),
            row["最后消息时间"],
        ),
        reverse=True,
    )
    write_csv(
        out_dir / "group_daily.csv",
        summaries,
        [
            "群聊", "消息数", "有效消息数", "发言人数", "活跃发言人", "最后消息时间", "主要主题",
            "商单信号数", "变现机会数", "商单机会数", "培训/项目合作机会数",
            "重要讨论数", "重要信号数", "项目合作信号数", "活动信号数",
            "立即处理讨论数",
            "招聘/外包信号数", "赚钱/奖励信号数", "讨论段落",
            "跨群高概率商单", "跨群疑似商单", "当前阶段", "建议动作",
            "群聊脉络", "关键发言", "商单/变现机会摘要", "商单机会摘要",
            "培训/项目合作摘要", "附件核验提示", "已有日报数", "群内已有日报", "机会信号数",
        ],
    )
    coverage = {
        "requested": len(sessions),
        "succeeded": succeeded_sessions,
        "failed": len(failed_sessions),
        "failed_sessions": failed_sessions,
        "incomplete": len(incomplete_sessions),
        "incomplete_sessions": incomplete_sessions,
        "since": since,
        "until": until,
        "reader_path": str(wechat_cli),
    }
    write_group_daily_digest(
        out_dir / "group_daily_digest.md",
        summaries,
        since,
        until,
        link_rows,
        coverage,
    )
    if getattr(args, "html", False):
        write_group_daily_html(
            out_dir / "group_daily_digest.html",
            summaries,
            since,
            until,
            link_rows,
            coverage,
        )
    write_group_editorial_packet(
        out_dir / "group_daily_editorial_packet.json",
        summaries,
        since,
        until,
        link_rows,
    )
    write_cross_group_link_report(out_dir, link_rows, since, until)
    write_group_selection_matrix(out_dir, summaries, all_messages, since, until)
    (out_dir / "group_daily.json").write_text(
        json.dumps({"since": since, "until": until, "groups": summaries, "cross_group_links": link_rows, "messages": [asdict(message) for message in all_messages]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "group_daily_coverage.json").write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    db_file = None
    if not args.no_db:
        db_file = store_group_daily_to_db(args.db, since, until, summaries, all_messages, out_dir)
    print(f"时间范围：{since} 至 {until}")
    print(f"活跃群聊：{len(sessions)} 个")
    if exclude_list:
        print(f"排除名单：{len(exclude_list)} 个，排除前群聊：{len(raw_sessions)} 个")
    if joined_channel_markers:
        print(f"已加入渠道标记：{len(joined_channel_markers)} 个")
    print(f"读取消息：{len(all_messages)} 条")
    if failed_sessions:
        print(f"读取失败：{len(failed_sessions)} 个群；详见 {out_dir / 'group_daily_coverage.json'}")
    if incomplete_sessions:
        print(f"覆盖不完整：{len(incomplete_sessions)} 个群仍有未读分页，请提高 --per-group-limit 后补读。")
    print(f"生成群聊摘要：{len(summaries)} 个")
    print(f"跨群/机会链接：{len(link_rows)} 个")
    print(f"语义编辑输入：{out_dir / 'group_daily_editorial_packet.json'}")
    if getattr(args, "html", False):
        print(f"机器版 HTML：{out_dir / 'group_daily_digest.html'}")
    if db_file:
        print(f"本地情报库：{db_file}")
    print(f"输出目录：{out_dir}")


def render_report_command(args: argparse.Namespace) -> None:
    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"找不到 Markdown 报告：{source}")

    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise SystemExit("生成 HTML 需要 pandoc。Markdown 报告不受影响；安装 pandoc 后重试即可。")

    output = Path(args.out).expanduser().resolve() if args.out else source.with_suffix(".html")
    require_sanitizer()
    css = Path(args.css).expanduser().resolve() if args.css else Path(__file__).resolve().parent / "assets" / "report.css"
    command = [
        pandoc,
        str(source),
        "--sandbox",
        "--from=gfm-raw_html",
        "--to=html5",
    ]
    result = subprocess.run(command, text=True, capture_output=True, timeout=60)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "pandoc 转换失败").strip()
        raise SystemExit(detail)
    fragment = sanitize_fragment(result.stdout)
    style = css.read_text(encoding="utf-8") if css.is_file() else ""
    style = re.sub(r"</style", r"<\\/style", style, flags=re.I)
    document = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{html_escape(args.title or source.stem)}</title><style>{style}</style>'
        f'</head><body><main>{fragment}</main></body></html>'
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(protect_document(document, static=True), encoding="utf-8")
    print(f"输入 Markdown：{source}")
    print(f"最终 HTML：{output}")


def render_bundle_command(args: argparse.Namespace) -> None:
    report_dir = Path(args.report_dir).expanduser().resolve()
    output = Path(args.out).expanduser().resolve() if args.out else None
    markdown_output = Path(args.markdown_out).expanduser().resolve() if args.markdown_out else None
    try:
        html_path, markdown_path, sources = render_report_bundle(
            report_dir,
            output_path=output,
            markdown_output_path=markdown_output,
            title=args.title,
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Markdown 报告入口：{markdown_path}")
    print(f"旗舰版交互 HTML：{html_path}")
    print(f"完整源报告：{len(sources)} 份；分区 Markdown 位于 {report_dir / 'wechat-report'}")
    unmapped = [source.relative_path for source in sources if source.kind == "other"]
    if unmapped:
        print(
            f"未进入 HTML 的 Markdown：{'、'.join(unmapped)}。"
            "需要展示时，在报告目录的 report_manifest.json 里为它指定 page"
            "（overview / groups / contacts / radar）后重新渲染。"
        )


def db_index(args: argparse.Namespace) -> None:
    wechat_cli = require_compatible_wechat_cli(args.wechat_cli)
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    since, until = resolve_time_window(None, args.until) if args.default_24h else (args.since, args.until)
    exclude_list = load_watchlist(args.exclude_list)
    messages: list[Message] = []
    target_rows: list[dict[str, Any]] = []
    raw_payloads: list[dict[str, Any]] = []

    if args.scope == "labels":
        labels = args.label or configured_labels("priority")
        targets = load_label_contact_targets(Path(args.contacts).expanduser(), labels)
        for target in targets[: args.target_limit]:
            session = {"display_name": target["display_name"], "username": target["username"]}
            try:
                chat_messages = timeline_messages_for_group(wechat_cli, session, since, until, args.per_chat_limit)
            except SystemExit as exc:
                target_rows.append(
                    {
                        "标签": target["label"],
                        "联系人": target["display_name"],
                        "username": target["username"],
                        "索引消息数": "0",
                        "最后消息时间": "",
                        "状态": shorten(str(exc), 120),
                    }
                )
                continue
            if not chat_messages:
                continue
            messages.extend(chat_messages)
            target_rows.append(
                {
                    "标签": target["label"],
                    "联系人": target["display_name"],
                    "username": target["username"],
                    "索引消息数": str(len(chat_messages)),
                    "最后消息时间": max(message.time for message in chat_messages),
                    "状态": "ok",
                }
            )
    elif args.scope == "sessions":
        sessions = fetch_sessions(wechat_cli, args.session_limit, args.session_type)
        sessions = filter_sessions_by_exclude_list(sessions, exclude_list)
        for session in sessions:
            try:
                chat_messages = timeline_messages_for_group(wechat_cli, session, since, until, args.per_chat_limit)
            except SystemExit as exc:
                target_rows.append(
                    {
                        "标签": str(session.get("chat_type") or args.session_type),
                        "联系人": str(session.get("display_name") or session.get("username") or ""),
                        "username": str(session.get("username") or ""),
                        "索引消息数": "0",
                        "最后消息时间": "",
                        "状态": shorten(str(exc), 120),
                    }
                )
                continue
            if not chat_messages:
                continue
            messages.extend(chat_messages)
            target_rows.append(
                {
                    "标签": str(session.get("chat_type") or args.session_type),
                    "联系人": str(session.get("display_name") or session.get("username") or ""),
                    "username": str(session.get("username") or ""),
                    "索引消息数": str(len(chat_messages)),
                    "最后消息时间": max(message.time for message in chat_messages),
                    "状态": "ok",
                }
            )
    else:
        keywords = args.keyword or DEFAULT_VAULT_KEYWORDS
        for keyword in keywords:
            offset = 0
            for _ in range(args.max_pages):
                command = [
                    str(wechat_cli),
                    "search",
                    keyword,
                    "--limit",
                    str(args.search_limit),
                    "--offset",
                    str(offset),
                    "--max-text-chars",
                    str(args.max_text_chars),
                ]
                if args.chat:
                    command.extend(["--in", args.chat])
                if since:
                    command.extend(["--after", since])
                if until:
                    command.extend(["--before", until])
                raw = run_json_command(command)
                raw_payloads.append({"keyword": keyword, "offset": offset, "raw": raw})
                page_messages = rows_to_messages(extract_json_rows(raw), f"wechat-cli:search:{keyword}:offset:{offset}")
                messages.extend(page_messages)
                query = extract_query(raw)
                if not query.get("has_more"):
                    break
                next_offset = query.get("next_offset")
                if not isinstance(next_offset, int) or next_offset <= offset:
                    break
                offset = next_offset
                if not page_messages:
                    break

    before_filters = len(messages)
    messages = dedupe_messages(messages)
    messages = filter_by_time(messages, since, until)
    messages = filter_by_exclude_list(messages, exclude_list)
    messages = [message for message in messages if message.content and not is_noise_evidence(message.content)]
    write_csv(out_dir / "indexed_targets.csv", target_rows, ["标签", "联系人", "username", "索引消息数", "最后消息时间", "状态"])
    (out_dir / "indexed_messages.json").write_text(json.dumps([asdict(message) for message in messages], ensure_ascii=False, indent=2), encoding="utf-8")
    if raw_payloads:
        (out_dir / "index_raw.json").write_text(json.dumps(raw_payloads, ensure_ascii=False, indent=2), encoding="utf-8")
    db_file, inserted_messages, inserted_links = store_messages_to_db(args.db, f"db_index_{args.scope}", since, until, messages, out_dir)
    print(f"索引范围：{args.scope}")
    print(f"时间范围：{since or '不限'} 至 {until or '不限'}")
    print(f"读取消息：{before_filters} 条")
    print(f"去重/过滤后：{len(messages)} 条")
    print(f"新写入消息：{inserted_messages} 条")
    print(f"新写入链接：{inserted_links} 条")
    print(f"本地情报库：{db_file}")
    print(f"输出目录：{out_dir}")


def scan(args: argparse.Namespace) -> None:
    input_paths = [Path(value).expanduser() for value in args.inputs]
    messages = load_messages(input_paths)
    watchlist = load_watchlist(args.watchlist)
    source_list = load_watchlist(args.source_list)
    exclude_list = load_watchlist(args.exclude_list)
    before_time_filter = len(messages)
    messages = filter_by_time(messages, args.since, args.until)
    before_list_filter = len(messages)
    messages = filter_by_lists(messages, watchlist, source_list)
    before_exclude_filter = len(messages)
    messages = filter_by_exclude_list(messages, exclude_list)
    out_dir = Path(args.out).expanduser()
    signals, deals = analyze_messages(messages, out_dir)
    db_file: Path | None = None
    inserted_messages = inserted_links = 0
    if getattr(args, "db", None):
        db_file, inserted_messages, inserted_links = store_messages_to_db(
            args.db,
            "file_scan",
            args.since,
            args.until,
            messages,
            out_dir,
        )

    print(f"读取消息：{len(messages)} 条")
    if args.since or args.until:
        print(f"时间范围：{args.since or '不限'} 至 {args.until or '不限'}，过滤前消息：{before_time_filter} 条")
    if watchlist:
        print(f"合作方名单：{len(watchlist)} 个，名单过滤前消息：{before_list_filter} 条")
    if source_list:
        print(f"资源群名单：{len(source_list)} 个，名单过滤前消息：{before_list_filter} 条")
    if exclude_list:
        print(f"排除名单：{len(exclude_list)} 个，排除前消息：{before_exclude_filter} 条")
    print(f"识别信号：{len(signals)} 条")
    print(f"聚合对象：{len(deals)} 个")
    if db_file:
        print(f"新写入消息：{inserted_messages} 条")
        print(f"新写入链接：{inserted_links} 条")
        print(f"本地情报库：{db_file}")
    print(f"输出目录：{out_dir}")


def run_vault_cli(vault_cli: Path, vault_args: list[str], python_bin: str) -> Any:
    command = [python_bin, str(vault_cli), *vault_args]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(
            "vault_cli 调用失败：\n"
            + " ".join(command)
            + "\n\nstderr:\n"
            + result.stderr.strip()
        )
    output = result.stdout.strip()
    if not output:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"text_output": output}


def rows_to_messages(rows: list[dict[str, Any]], source: str) -> list[Message]:
    messages: list[Message] = []
    for row in rows:
        content = first_value(row, ["content", "text", "match", "message", "msg", "body", "summary"])
        if not content:
            continue
        chat = first_value(row, ["chat", "chat_name", "room", "room_name", "contact", "talker", "session", "name"]) or "未命名会话"
        talker = first_value(row, ["talker", "chat_id", "username"])
        if isinstance(chat, dict):
            talker = first_value(chat, ["username", "talker", "chat_id"]) or talker
            chat = first_value(chat, ["display_name", "name", "talker", "username"]) or "未命名会话"
        sender = first_value(row, ["sender", "from", "speaker", "nickname", "user", "sender_name"]) or chat
        time = first_value(row, ["time", "time_iso", "create_time", "created_at", "datetime", "date", "msg_time"]) or ""
        if isinstance(time, (int, float)):
            time = datetime.fromtimestamp(time).strftime("%Y-%m-%d %H:%M:%S")
        messages.append(
            Message(
                chat=str(chat).strip(),
                sender=str(sender).strip(),
                time=normalize_time(str(time)),
                content=str(content).strip(),
                source_file=f"wechat-cli:timeline:{talker}" if source.startswith("wechat-cli:") and isinstance(talker, str) and talker.strip() else source,
            )
        )
    return messages


def run_json_command(command: list[str]) -> Any:
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(
            "命令调用失败：\n"
            + " ".join(command)
            + "\n\nstderr:\n"
            + result.stderr.strip()
        )
    output = result.stdout.strip()
    if not output:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"命令没有返回 JSON：{' '.join(command)}\n{exc}\n{output[:500]}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_wechat_app_info(app_path_value: str = DEFAULT_WECHAT_APP) -> dict[str, str]:
    app_path = Path(app_path_value).expanduser()
    info_path = app_path / "Contents" / "Info.plist"
    info: dict[str, Any] = {}
    if info_path.exists():
        try:
            with info_path.open("rb") as handle:
                loaded = plistlib.load(handle)
                if isinstance(loaded, dict):
                    info = loaded
        except (OSError, plistlib.InvalidFileException):
            info = {}
    return {
        "path": str(app_path),
        "version": str(info.get("CFBundleShortVersionString") or "unknown"),
        "build": str(info.get("CFBundleVersion") or "unknown"),
        "bundle_version": str(info.get("WeChatBundleVersion") or "unknown"),
    }


def read_reader_info(wechat_cli: Path) -> dict[str, str]:
    resolved = wechat_cli.resolve() if wechat_cli.exists() else wechat_cli
    version = "unknown"
    if wechat_cli.exists():
        raw = run_json_command([str(wechat_cli), "version"])
        data = raw.get("data", {}) if isinstance(raw, dict) else {}
        if isinstance(data, dict):
            version = str(data.get("version") or data.get("name") or "unknown")
    return {
        "path": str(resolved),
        "version": version,
        "sha256": file_sha256(resolved) if resolved.is_file() else "",
    }


def compatibility_signature(report: dict[str, Any]) -> tuple[str, ...]:
    wechat = report.get("wechat") if isinstance(report.get("wechat"), dict) else {}
    reader = report.get("reader") if isinstance(report.get("reader"), dict) else {}
    return (
        str(wechat.get("version") or ""),
        str(wechat.get("build") or ""),
        str(wechat.get("bundle_version") or ""),
        str(reader.get("version") or ""),
        str(reader.get("sha256") or ""),
    )


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def compatibility_cache_is_fresh(
    previous: dict[str, Any] | None,
    current_identity: dict[str, Any],
    max_age_hours: float,
) -> bool:
    if not previous or previous.get("result") not in {"ready", "degraded"}:
        return False
    if compatibility_signature(previous) != compatibility_signature(current_identity):
        return False
    try:
        checked_at = datetime.fromisoformat(str(previous.get("checked_at") or ""))
    except ValueError:
        return False
    now = datetime.now().astimezone()
    if checked_at.tzinfo is None:
        checked_at = checked_at.astimezone()
    return now - checked_at <= timedelta(hours=max_age_hours)


def sanitized_compat_error(exc: BaseException) -> str:
    value = str(exc).replace(str(Path.home()), "~")
    value = re.sub(r"wxid_[\w-]+", "<wechat-id>", value)
    value = re.sub(r"\S+@chatroom", "<chatroom-id>", value)
    return shorten(value.replace("\n", " "), 240)


def run_compatibility_check(
    wechat_cli_value: str,
    *,
    wechat_app_value: str = DEFAULT_WECHAT_APP,
    state_dir_value: str = DEFAULT_COMPAT_DIR,
    force: bool = False,
    max_age_hours: float = 6,
) -> dict[str, Any]:
    wechat_cli = Path(wechat_cli_value).expanduser()
    state_dir = Path(state_dir_value).expanduser()
    latest_path = state_dir / "latest.json"
    previous = read_json_file(latest_path)
    errors: list[str] = []

    wechat_info = read_wechat_app_info(wechat_app_value)
    try:
        reader_info = read_reader_info(wechat_cli)
    except SystemExit as exc:
        reader_info = {
            "path": str(wechat_cli),
            "version": "unknown",
            "sha256": file_sha256(wechat_cli) if wechat_cli.is_file() else "",
        }
        errors.append("reader-version: " + sanitized_compat_error(exc))

    identity = {"wechat": wechat_info, "reader": reader_info}
    if not force and compatibility_cache_is_fresh(previous, identity, max_age_hours):
        cached = dict(previous or {})
        cached["cache_reused"] = True
        return cached

    checks: dict[str, Any] = {
        "status": {"ok": False, "live_read_ok": False, "readiness": "unknown"},
        "sessions": {"ok": False, "count": 0},
        "timeline": {"ok": False, "attempted": False, "count": 0},
    }
    capabilities: dict[str, bool] = {}
    status_warnings: list[str] = []

    if not wechat_cli.exists():
        errors.append(f"reader-missing: {wechat_cli}")
    else:
        try:
            raw_status = run_json_command([str(wechat_cli), "--strict-read-only", "status"])
            data = raw_status.get("data", {}) if isinstance(raw_status, dict) else {}
            status = data.get("status", data) if isinstance(data, dict) else {}
            if not isinstance(status, dict):
                status = {}
            raw_capabilities = status.get("capabilities", {})
            if isinstance(raw_capabilities, dict):
                capabilities = {str(key): bool(value) for key, value in raw_capabilities.items()}
            raw_warnings = status.get("warnings", [])
            if isinstance(raw_warnings, list):
                status_warnings = [str(item) for item in raw_warnings]
            checks["status"] = {
                "ok": True,
                "live_read_ok": bool(status.get("live_read_ok")),
                "readiness": str(status.get("readiness") or "unknown"),
            }
        except SystemExit as exc:
            errors.append("status: " + sanitized_compat_error(exc))

        sessions: list[dict[str, Any]] = []
        try:
            raw_sessions = run_json_command(
                [str(wechat_cli), "--strict-read-only", "sessions", "--limit", "3"]
            )
            sessions = extract_json_rows(raw_sessions)
            checks["sessions"] = {"ok": True, "count": len(sessions)}
        except SystemExit as exc:
            errors.append("sessions: " + sanitized_compat_error(exc))

        smoke_session = next(
            (
                row
                for row in sessions
                if str(row.get("chat_type") or "").lower() in {"private", "group", "official_account"}
                if first_value(row, ["username", "talker", "chatroom_id", "session_id"])
            ),
            None,
        )
        if smoke_session:
            checks["timeline"]["attempted"] = True
            username = str(first_value(smoke_session, ["username", "talker", "chatroom_id", "session_id"]))
            try:
                raw_timeline = run_json_command(
                    [
                        str(wechat_cli),
                        "--strict-read-only",
                        "timeline",
                        username,
                        "--limit",
                        "1",
                        "--include-media-paths",
                        "false",
                    ]
                )
                checks["timeline"] = {
                    "ok": True,
                    "attempted": True,
                    "count": len(extract_json_rows(raw_timeline)),
                }
            except SystemExit as exc:
                errors.append("timeline: " + sanitized_compat_error(exc).replace(username, "<session-id>"))

    status_ok = checks["status"]["ok"] and checks["status"]["live_read_ok"]
    sessions_ok = checks["sessions"]["ok"]
    timeline_ok = checks["timeline"]["ok"] if checks["timeline"]["attempted"] else False
    core_ok = status_ok and sessions_ok and timeline_ok
    readiness = checks["status"]["readiness"]
    missing_capabilities = [name for name in ("sessions", "timeline", "search") if not capabilities.get(name)]
    if core_ok and readiness == "ready" and not status_warnings and not missing_capabilities:
        result = "ready"
    elif core_ok:
        result = "degraded"
    else:
        result = "blocked"

    current_signature = compatibility_signature(identity)
    previous_signature = compatibility_signature(previous) if previous else ()
    version_changed = bool(previous and current_signature != previous_signature)
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    report: dict[str, Any] = {
        "schema_version": 1,
        "checked_at": checked_at,
        "result": result,
        "version_changed": version_changed,
        "cache_reused": False,
        "wechat": wechat_info,
        "reader": reader_info,
        "checks": checks,
        "capabilities": capabilities,
        "warnings": status_warnings,
        "missing_capabilities": missing_capabilities,
        "errors": errors,
        "next_action": (
            "实时只读通道可用，可继续生成日报和检索。"
            if result == "ready"
            else "实时读取可用，但存在非核心警告；可继续使用并关注后续 CLI 更新。"
            if result == "degraded"
            else "已阻止实时读取。先用 db-search 查历史情报库，更新读取适配器后重跑 compat-check。"
        ),
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    history_dir = state_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    latest_path.write_text(payload, encoding="utf-8")
    history_name = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f.json")
    (history_dir / history_name).write_text(payload, encoding="utf-8")
    return report


def require_compatible_wechat_cli(wechat_cli_value: str) -> Path:
    wechat_cli = Path(wechat_cli_value).expanduser()
    report = run_compatibility_check(str(wechat_cli))
    if report.get("result") == "blocked":
        latest_path = Path(DEFAULT_COMPAT_DIR).expanduser() / "latest.json"
        raise SystemExit(
            "微信实时只读兼容检查未通过，已安全停止，不会生成残缺日报。\n"
            f"兼容报告：{latest_path}\n"
            "历史记录仍可用：python3 wechat_intelligence_hub.py db-search <关键词>"
        )
    return wechat_cli


def compat_check(args: argparse.Namespace) -> None:
    report = run_compatibility_check(
        args.wechat_cli,
        wechat_app_value=args.wechat_app,
        state_dir_value=args.state_dir,
        force=args.force,
        max_age_hours=args.max_age_hours,
    )
    latest_path = Path(args.state_dir).expanduser() / "latest.json"
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    labels = {"ready": "通过", "degraded": "可用（有警告）", "blocked": "已阻止"}
    print(f"兼容检查：{labels.get(str(report.get('result')), report.get('result'))}")
    print(
        f"微信：{report['wechat']['version']} (build {report['wechat']['build']})；"
        f"wechat-cli：{report['reader']['version']}"
    )
    if report.get("version_changed"):
        print("检测到微信或读取器版本变化，已重新完成实读测试。")
    print(f"建议：{report['next_action']}")
    print(f"报告：{latest_path}")
    if args.out:
        print(f"报告副本：{Path(args.out).expanduser()}")
    if report.get("result") == "blocked":
        raise SystemExit(2)


def read_varint(buffer: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(buffer):
        byte = buffer[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("bad protobuf varint")


def parse_extra_buffer_labels(hex_value: str | None) -> list[str]:
    if not hex_value:
        return []
    try:
        buffer = bytes.fromhex(hex_value)
    except ValueError:
        return []

    offset = 0
    labels: list[str] = []
    while offset < len(buffer):
        try:
            key, offset = read_varint(buffer, offset)
        except ValueError:
            break
        field_number = key >> 3
        wire_type = key & 7

        if wire_type == 0:
            try:
                _, offset = read_varint(buffer, offset)
            except ValueError:
                break
        elif wire_type == 1:
            offset += 8
        elif wire_type == 2:
            try:
                length, offset = read_varint(buffer, offset)
            except ValueError:
                break
            value = buffer[offset : offset + length]
            offset += length
            if field_number == 30:
                try:
                    label_text = value.decode("utf-8").strip()
                except UnicodeDecodeError:
                    label_text = ""
                labels.extend([item.strip() for item in label_text.split(",") if item.strip()])
        elif wire_type == 5:
            offset += 4
        else:
            break

    return labels


def contact_display_name(row: dict[str, Any]) -> str:
    return str(first_value(row, ["remark", "nick_name", "alias", "username"]) or "").strip()


def contact_match_names(row: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("remark", "nick_name", "alias", "username"):
        value = str(row.get(key) or "").strip()
        if value and value not in names:
            names.append(value)
    return names


def load_label_contact_targets(csv_path: Path, labels: list[str]) -> list[dict[str, Any]]:
    wanted = set(labels)
    if not csv_path.exists():
        raise SystemExit(f"找不到标签联系人 CSV：{csv_path}")
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            label = str(row.get("标签") or "").strip()
            if wanted and label not in wanted:
                continue
            username = str(row.get("username") or "").strip()
            display_name = str(row.get("显示名") or row.get("备注") or row.get("昵称") or username).strip()
            if not username and not display_name:
                continue
            key = username or display_name
            if key in seen:
                continue
            seen.add(key)
            targets.append({"username": username or display_name, "display_name": display_name, "label": label})
    return targets


def load_label_contact_rows(csv_path: Path, labels: list[str]) -> list[dict[str, Any]]:
    wanted = set(labels)
    if not csv_path.exists():
        raise SystemExit(f"找不到标签联系人 CSV：{csv_path}")
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            label = str(row.get("标签") or "").strip()
            if wanted and label not in wanted:
                continue
            username = str(row.get("username") or "").strip()
            display_name = str(row.get("显示名") or row.get("备注") or row.get("昵称") or username).strip()
            names = unique_items(
                str(row.get(key) or "").strip()
                for key in ("显示名", "备注", "昵称", "微信号/alias", "username")
            )
            names = [name for name in names if name]
            if not username and not display_name and not names:
                continue
            key = username or display_name or "|".join(names)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                {
                    "label": label,
                    "username": username,
                    "display_name": display_name or username or names[0],
                    "names": names,
                }
            )
    return targets


def fetch_wechat_labels(wechat_cli: Path) -> dict[str, str]:
    command = [
        str(wechat_cli),
        "sql",
        "select label_id_, label_name_ from contact_label",
        "--subdir",
        "contact",
        "--file",
        "contact.db",
        "--limit",
        "1000",
    ]
    rows = extract_json_rows(run_json_command(command))
    return {str(row.get("label_id_")): str(row.get("label_name_")) for row in rows if row.get("label_id_") and row.get("label_name_")}


def fetch_wechat_contacts_with_labels(wechat_cli: Path) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    offset = 0
    page_size = 1000
    while True:
        query = (
            "select username, remark, nick_name, alias, local_type, flag, delete_flag, verify_flag, "
            "length(extra_buffer) as extra_buffer_len, hex(extra_buffer) as extra_buffer_hex "
            "from contact where delete_flag=0 and length(extra_buffer)>0 "
            f"limit {page_size} offset {offset}"
        )
        command = [
            str(wechat_cli),
            "sql",
            query,
            "--subdir",
            "contact",
            "--file",
            "contact.db",
            "--limit",
            str(page_size),
        ]
        rows = extract_json_rows(run_json_command(command))
        for row in rows:
            label_ids = parse_extra_buffer_labels(str(row.get("extra_buffer_hex") or ""))
            if label_ids:
                row["label_ids"] = label_ids
                contacts.append(row)
        if len(rows) < page_size:
            break
        offset += page_size
    return contacts


def write_name_list(path: Path, names: list[str], title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    unique_names = sorted({name.strip() for name in names if name.strip()}, key=str.lower)
    lines = [f"# {title}", f"# 自动从本机微信联系人标签生成，共 {len(unique_names)} 个匹配名。", ""]
    lines.extend(unique_names)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def wechat_labels(args: argparse.Namespace) -> None:
    wechat_cli = require_compatible_wechat_cli(args.wechat_cli)

    output_dir = Path(args.out).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    wanted_label_names = args.label or configured_labels("priority")
    if not wanted_label_names:
        raise SystemExit(
            "尚未配置重点微信标签。先运行 profile-init --inspect-wechat-labels，"
            "或用 --label 明确指定要导出的标签。"
        )

    labels = fetch_wechat_labels(wechat_cli)
    wanted_ids = {label_id for label_id, label_name in labels.items() if label_name in wanted_label_names}
    missing = [label_name for label_name in wanted_label_names if label_name not in labels.values()]
    if missing:
        raise SystemExit("微信里找不到这些标签：" + "、".join(missing))

    contacts = fetch_wechat_contacts_with_labels(wechat_cli)
    rows: list[dict[str, str]] = []
    names_by_label = {label_name: [] for label_name in wanted_label_names}
    combined_names: list[str] = []

    for contact in contacts:
        matched_ids = [label_id for label_id in contact.get("label_ids", []) if label_id in wanted_ids]
        if not matched_ids:
            continue
        match_names = contact_match_names(contact)
        combined_names.extend(match_names)
        for label_id in matched_ids:
            label_name = labels[label_id]
            names_by_label.setdefault(label_name, []).extend(match_names)
            rows.append(
                {
                    "标签": label_name,
                    "显示名": contact_display_name(contact),
                    "备注": str(contact.get("remark") or ""),
                    "昵称": str(contact.get("nick_name") or ""),
                    "微信号/alias": str(contact.get("alias") or ""),
                    "username": str(contact.get("username") or ""),
                    "标签ID": label_id,
                }
            )

    for label_name, names in names_by_label.items():
        write_name_list(output_dir / f"{label_name}名单.txt", names, label_name)
    write_name_list(output_dir / "重点联系人名单.txt", combined_names, " + ".join(wanted_label_names))
    write_csv(
        output_dir / "微信标签联系人.csv",
        rows,
        ["标签", "显示名", "备注", "昵称", "微信号/alias", "username", "标签ID"],
    )

    print(f"读取标签：{len(labels)} 个")
    print(f"解析带标签联系人：{len(contacts)} 个")
    for label_name in wanted_label_names:
        unique_count = len({name for name in names_by_label.get(label_name, []) if name})
        contact_count = sum(1 for row in rows if row["标签"] == label_name)
        print(f"{label_name}：{contact_count} 个联系人，{unique_count} 个匹配名")
    print(f"输出目录：{output_dir}")


def extract_query(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, dict) and isinstance(data.get("query"), dict):
            return data["query"]
        if isinstance(raw.get("query"), dict):
            return raw["query"]
    return {}


def dedupe_messages(messages: list[Message]) -> list[Message]:
    seen: set[str] = set()
    unique: list[Message] = []
    for message in messages:
        key = message_hash(message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(message)
    return unique


def vault_status(args: argparse.Namespace) -> None:
    vault_cli = Path(args.vault_cli).expanduser()
    if not vault_cli.exists():
        raise SystemExit(f"找不到 vault_cli.py：{vault_cli}")
    raw = run_vault_cli(vault_cli, ["status", "--format", "json"], args.python)
    print(json.dumps(raw, ensure_ascii=False, indent=2))


def vault_scan(args: argparse.Namespace) -> None:
    vault_cli = Path(args.vault_cli).expanduser()
    if not vault_cli.exists():
        raise SystemExit(f"找不到 vault_cli.py：{vault_cli}")

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_payloads: list[dict[str, Any]] = []
    messages: list[Message] = []

    if args.mode == "new-messages":
        raw = run_vault_cli(vault_cli, ["new-messages", "--format", "json"], args.python)
        raw_payloads.append({"mode": args.mode, "raw": raw})
        messages.extend(rows_to_messages(extract_json_rows(raw), "vault:new-messages"))
    else:
        keywords = args.keyword or DEFAULT_VAULT_KEYWORDS
        for keyword in keywords:
            command = ["search", keyword, "--format", "json"]
            if args.chat:
                command.extend(["--chat", args.chat])
            raw = run_vault_cli(vault_cli, command, args.python)
            raw_payloads.append({"mode": args.mode, "keyword": keyword, "raw": raw})
            messages.extend(rows_to_messages(extract_json_rows(raw), f"vault:search:{keyword}"))

    messages = dedupe_messages(messages)
    watchlist = load_watchlist(args.watchlist)
    source_list = load_watchlist(args.source_list)
    exclude_list = load_watchlist(args.exclude_list)
    before_time_filter = len(messages)
    messages = filter_by_time(messages, args.since, args.until)
    before_list_filter = len(messages)
    messages = filter_by_lists(messages, watchlist, source_list)
    before_exclude_filter = len(messages)
    messages = filter_by_exclude_list(messages, exclude_list)
    (out_dir / "vault_raw.json").write_text(json.dumps(raw_payloads, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "vault_messages.json").write_text(json.dumps([asdict(message) for message in messages], ensure_ascii=False, indent=2), encoding="utf-8")
    signals, deals = analyze_messages(messages, out_dir)
    db_file: Path | None = None
    inserted_messages = inserted_links = 0
    if not getattr(args, "no_db", False):
        db_file, inserted_messages, inserted_links = store_messages_to_db(
            args.db,
            "vault_scan",
            args.since,
            args.until,
            messages,
            out_dir,
        )

    print(f"vault 消息：{len(messages)} 条")
    if args.since or args.until:
        print(f"时间范围：{args.since or '不限'} 至 {args.until or '不限'}，过滤前 vault 消息：{before_time_filter} 条")
    if watchlist:
        print(f"合作方名单：{len(watchlist)} 个，名单过滤前 vault 消息：{before_list_filter} 条")
    if source_list:
        print(f"资源群名单：{len(source_list)} 个，名单过滤前 vault 消息：{before_list_filter} 条")
    if exclude_list:
        print(f"排除名单：{len(exclude_list)} 个，排除前 vault 消息：{before_exclude_filter} 条")
    print(f"识别信号：{len(signals)} 条")
    print(f"聚合对象：{len(deals)} 个")
    if db_file:
        print(f"新写入消息：{inserted_messages} 条")
        print(f"新写入链接：{inserted_links} 条")
        print(f"本地情报库：{db_file}")
    print(f"输出目录：{out_dir}")


def wechat_scan(args: argparse.Namespace) -> None:
    wechat_cli = require_compatible_wechat_cli(args.wechat_cli)

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_payloads: list[dict[str, Any]] = []
    messages: list[Message] = []
    keywords = args.keyword or DEFAULT_VAULT_KEYWORDS

    for keyword in keywords:
        offset = 0
        for page in range(args.max_pages):
            command = [
                str(wechat_cli),
                "search",
                keyword,
                "--limit",
                str(args.limit),
                "--offset",
                str(offset),
                "--max-text-chars",
                str(args.max_text_chars),
            ]
            if args.chat:
                command.extend(["--in", args.chat])
            if args.since:
                command.extend(["--after", args.since])
            if args.until:
                command.extend(["--before", args.until])
            raw = run_json_command(command)
            raw_payloads.append({"keyword": keyword, "offset": offset, "raw": raw})
            page_messages = rows_to_messages(extract_json_rows(raw), f"wechat-cli:search:{keyword}:offset:{offset}")
            messages.extend(page_messages)

            query = extract_query(raw)
            if not query.get("has_more"):
                break
            next_offset = query.get("next_offset")
            if not isinstance(next_offset, int) or next_offset <= offset:
                break
            offset = next_offset
            if len(page_messages) == 0:
                break

    messages = dedupe_messages(messages)
    watchlist = load_watchlist(args.watchlist)
    source_list = load_watchlist(args.source_list)
    exclude_list = load_watchlist(args.exclude_list)
    before_time_filter = len(messages)
    messages = filter_by_time(messages, args.since, args.until)
    before_list_filter = len(messages)
    messages = filter_by_lists(messages, watchlist, source_list)
    before_exclude_filter = len(messages)
    messages = filter_by_exclude_list(messages, exclude_list)
    (out_dir / "wechat_raw.json").write_text(json.dumps(raw_payloads, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "wechat_messages.json").write_text(json.dumps([asdict(message) for message in messages], ensure_ascii=False, indent=2), encoding="utf-8")
    signals, deals = analyze_messages(messages, out_dir)
    db_file: Path | None = None
    inserted_messages = inserted_links = 0
    if not getattr(args, "no_db", False):
        db_file, inserted_messages, inserted_links = store_messages_to_db(
            args.db,
            "wechat_scan",
            args.since,
            args.until,
            messages,
            out_dir,
        )

    print(f"wechat-cli 消息：{len(messages)} 条")
    print(f"关键词：{len(keywords)} 个，每词最多 {args.limit * args.max_pages} 条")
    if args.since or args.until:
        print(f"时间范围：{args.since or '不限'} 至 {args.until or '不限'}，过滤前消息：{before_time_filter} 条")
    if watchlist:
        print(f"合作方名单：{len(watchlist)} 个，名单过滤前消息：{before_list_filter} 条")
    if source_list:
        print(f"资源群名单：{len(source_list)} 个，名单过滤前消息：{before_list_filter} 条")
    if exclude_list:
        print(f"排除名单：{len(exclude_list)} 个，排除前消息：{before_exclude_filter} 条")
    print(f"识别信号：{len(signals)} 条")
    print(f"聚合对象：{len(deals)} 个")
    if db_file:
        print(f"新写入消息：{inserted_messages} 条")
        print(f"新写入链接：{inserted_links} 条")
        print(f"本地情报库：{db_file}")
    print(f"输出目录：{out_dir}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地微信个人情报库：支持联系人、主题、简报、聊天检索和商单推进")
    default_exclude_list = existing_optional_list("contacts/排除名单.txt")
    default_joined_channel_list = existing_optional_list("contacts/已加入渠道.txt")
    parser.add_argument(
        "--profile",
        help="个人 Profile JSON；也可设置 WECHAT_HUB_PROFILE，默认自动读取 config/profile.local.json",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    profile_init_parser = subparsers.add_parser(
        "profile-init",
        help="根据个人说明、当前计划和微信标签初始化本地个性化 Profile",
    )
    profile_init_parser.add_argument("--out", default="config/profile.local.json", help="Profile 输出路径")
    profile_init_parser.add_argument("--report", default="output/onboarding/profile_setup.md", help="初始化报告路径")
    profile_init_parser.add_argument("--owner-alias", action="append", help="本人微信昵称或别名，可重复")
    profile_init_parser.add_argument("--social-handle", action="append", help="本人公开账号用户名，可重复")
    profile_init_parser.add_argument("--personal-doc", action="append", help="个人说明文件或目录，可重复")
    profile_init_parser.add_argument("--plan-doc", action="append", help="当前计划文件或目录，可重复")
    profile_init_parser.add_argument("--focus", action="append", help="重点方向，可重复；不填时从文档识别通用方向")
    profile_init_parser.add_argument("--priority-keyword", action="append", help="必须优先留意的个人关键词，可重复")
    profile_init_parser.add_argument("--deprioritize-keyword", action="append", help="没有行动信号时应降级的关键词，可重复")
    profile_init_parser.add_argument(
        "--custom-topic",
        action="append",
        help="自定义主题，格式：主题名=关键词1,关键词2；可重复",
    )
    profile_init_parser.add_argument("--priority-label", action="append", help="需要优先扫描的微信标签，可重复")
    profile_init_parser.add_argument("--commercial-label", action="append", help="客户/品牌/商务类微信标签，可重复")
    profile_init_parser.add_argument("--creator-label", action="append", help="同行/创作者/资源类微信标签，可重复")
    profile_init_parser.add_argument("--reactivation-label", action="append", help="需要定期复联的微信标签，可重复")
    profile_init_parser.add_argument("--inspect-wechat-labels", action="store_true", help="只读列出本机现有微信标签并给候选建议")
    profile_init_parser.add_argument("--wechat-cli", default=DEFAULT_WECHAT_READER, help="只读 Reader 路径")
    profile_init_parser.add_argument("--force", action="store_true", help="在保留原字段基础上更新已存在 Profile")
    profile_init_parser.set_defaults(func=profile_init_command)

    profile_status_parser = subparsers.add_parser("profile-status", help="检查个性化 Profile、计划文档和重点标签是否齐全")
    profile_status_parser.add_argument("--json", action="store_true", help="输出 JSON")
    profile_status_parser.set_defaults(func=profile_status_command)

    compat_parser = subparsers.add_parser("compat-check", help="微信升级后检查只读通道是否仍可用")
    compat_parser.add_argument("--wechat-cli", default=DEFAULT_WECHAT_READER, help="只读 Reader 路径")
    compat_parser.add_argument("--wechat-app", default=DEFAULT_WECHAT_APP, help="微信 App 路径")
    compat_parser.add_argument("--state-dir", default=DEFAULT_COMPAT_DIR, help="兼容报告和历史记录目录")
    compat_parser.add_argument("--force", action="store_true", help="忽略缓存，立即运行严格只读实读测试")
    compat_parser.add_argument("--max-age-hours", type=float, default=6, help="相同版本的检查结果最多缓存几小时")
    compat_parser.add_argument("--out", help="额外写出一份 JSON 报告")
    compat_parser.set_defaults(func=compat_check)

    scan_parser = subparsers.add_parser("scan", help="扫描聊天导出文件并生成 CSV/Markdown")
    scan_parser.add_argument("inputs", nargs="+", help="聊天文本或 JSON 文件")
    scan_parser.add_argument("--out", default="output", help="输出目录")
    scan_parser.add_argument("--watchlist", help="合作方名单 txt，一行一个名字；只保留匹配聊天对象/发言人的消息")
    scan_parser.add_argument("--source-list", help="资源群/博主好友名单 txt，一行一个名字；按聊天对象匹配")
    scan_parser.add_argument("--exclude-list", help="排除名单 txt，一行一个名字；匹配聊天对象/发言人即跳过")
    scan_parser.add_argument("--since", help="只保留这个时间之后的消息，例如 2025-11-01")
    scan_parser.add_argument("--until", help="只保留这个时间之前的消息，例如 2026-07-02")
    scan_parser.add_argument("--db", help="可选：同步写入本地情报库和商机管线")
    scan_parser.set_defaults(func=scan)

    status_parser = subparsers.add_parser("vault-status", help="检查 wechat-local-vault 的只读状态")
    status_parser.add_argument("--vault-cli", required=True, help="vault_cli.py 的路径")
    status_parser.add_argument("--python", default=sys.executable or "python3", help="运行 vault_cli.py 的 Python")
    status_parser.set_defaults(func=vault_status)

    vault_parser = subparsers.add_parser("vault-scan", help="调用 vault_cli.py 读取本地微信 vault 并生成商单摘要")
    vault_parser.add_argument("--vault-cli", required=True, help="vault_cli.py 的路径")
    vault_parser.add_argument("--mode", choices=["search", "new-messages"], default="search", help="读取模式")
    vault_parser.add_argument("--keyword", action="append", help="搜索关键词，可重复；默认使用商单关键词包")
    vault_parser.add_argument("--chat", help="只扫描指定联系人或群聊")
    vault_parser.add_argument("--watchlist", help="合作方名单 txt，一行一个名字；只保留匹配聊天对象/发言人的消息")
    vault_parser.add_argument("--source-list", help="资源群/博主好友名单 txt，一行一个名字；按聊天对象匹配")
    vault_parser.add_argument("--exclude-list", help="排除名单 txt，一行一个名字；匹配聊天对象/发言人即跳过")
    vault_parser.add_argument("--since", help="只保留这个时间之后的消息，例如 2025-11-01")
    vault_parser.add_argument("--until", help="只保留这个时间之前的消息，例如 2026-07-02")
    vault_parser.add_argument("--out", default="output/vault-today", help="输出目录")
    vault_parser.add_argument("--python", default=sys.executable or "python3", help="运行 vault_cli.py 的 Python")
    vault_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    vault_parser.add_argument("--no-db", action="store_true", help="只生成文件，不写入情报库和商机管线")
    vault_parser.set_defaults(func=vault_scan)

    wechat_parser = subparsers.add_parser("wechat-scan", help="调用 rion-wechat-cli 只读检索本地微信并生成情报摘要")
    wechat_parser.add_argument("--wechat-cli", default=DEFAULT_WECHAT_READER, help="只读 Reader 路径")
    wechat_parser.add_argument("--keyword", action="append", help="搜索关键词，可重复；默认使用商单关键词包")
    wechat_parser.add_argument("--chat", help="只扫描指定联系人或群聊")
    wechat_parser.add_argument("--watchlist", help="合作方名单 txt，一行一个名字；只保留匹配聊天对象/发言人的消息")
    wechat_parser.add_argument("--source-list", help="资源群/博主好友名单 txt，一行一个名字；按聊天对象匹配")
    wechat_parser.add_argument("--exclude-list", help="排除名单 txt，一行一个名字；匹配聊天对象/发言人即跳过")
    wechat_parser.add_argument("--since", help="只保留这个时间之后的消息，例如 2025-11-01")
    wechat_parser.add_argument("--until", help="只保留这个时间之前的消息，例如 2026-07-02")
    wechat_parser.add_argument("--limit", type=int, default=100, help="每页搜索结果数，wechat-cli 最大 1000")
    wechat_parser.add_argument("--max-pages", type=int, default=5, help="每个关键词最多翻几页")
    wechat_parser.add_argument("--max-text-chars", type=int, default=1000, help="每条消息最多保留多少字符")
    wechat_parser.add_argument("--out", default="output/wechat-today", help="输出目录")
    wechat_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    wechat_parser.add_argument("--no-db", action="store_true", help="只生成文件，不写入情报库和商机管线")
    wechat_parser.set_defaults(func=wechat_scan)

    labels_parser = subparsers.add_parser("wechat-labels", help="从本机微信联系人标签生成扫描名单")
    labels_parser.add_argument("--wechat-cli", default=DEFAULT_WECHAT_READER, help="只读 Reader 路径")
    labels_parser.add_argument("--label", action="append", help="要导出的微信标签，可重复；默认读取个人 Profile 的重点标签")
    labels_parser.add_argument("--out", default="contacts", help="名单输出目录")
    labels_parser.set_defaults(func=wechat_labels)

    priority_parser = subparsers.add_parser("prioritize", help="按你的商单工作流重排 signals.json")
    priority_parser.add_argument("signals", help="wechat-scan 或 scan 生成的 signals.json")
    priority_parser.add_argument("--contacts", default="contacts/微信标签联系人.csv", help="wechat-labels 生成的标签联系人 CSV")
    priority_parser.add_argument("--out", default="output/priority-workbench", help="输出目录")
    priority_parser.set_defaults(func=prioritize)

    daily_parser = subparsers.add_parser("daily", help="一键同步标签、扫描微信、生成个人商单机会工作台")
    daily_parser.add_argument("--wechat-cli", default=DEFAULT_WECHAT_READER, help="只读 Reader 路径")
    daily_parser.add_argument("--label", action="append", help="要同步的微信标签，可重复；默认读取个人 Profile 的重点标签")
    daily_parser.add_argument("--contacts-dir", default="contacts", help="联系人名单输出目录")
    daily_parser.add_argument("--scan-out", default="output/wechat-latest", help="原始扫描输出目录")
    daily_parser.add_argument("--out", default="output/personal-workbench", help="个人工作台输出目录")
    daily_parser.add_argument("--keyword", action="append", help="搜索关键词，可重复；默认使用商单关键词包")
    daily_parser.add_argument("--chat", help="只扫描指定联系人或群聊")
    daily_parser.add_argument("--source-list", help="额外资源群/博主好友名单 txt")
    daily_parser.add_argument("--exclude-list", default=default_exclude_list, help="排除名单 txt")
    daily_parser.add_argument("--since", default="2025-11-01", help="只保留这个时间之后的消息")
    daily_parser.add_argument("--until", help="只保留这个时间之前的消息")
    daily_parser.add_argument("--limit", type=int, default=100, help="每页搜索结果数")
    daily_parser.add_argument("--max-pages", type=int, default=3, help="每个关键词最多翻几页；日常默认 3，深度复盘可设 5")
    daily_parser.add_argument("--max-text-chars", type=int, default=1000, help="每条消息最多保留多少字符")
    daily_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    daily_parser.add_argument("--no-db", action="store_true", help="只生成文件，不更新情报库和商机管线")
    daily_parser.set_defaults(func=daily)

    reactivation_parser = subparsers.add_parser("reactivation", help="分析客户/合作方标签联系人，找出值得重新建联或复购保温的关系")
    reactivation_parser.add_argument("--wechat-cli", default=DEFAULT_WECHAT_READER, help="只读 Reader 路径；只在 --index-first 时使用")
    reactivation_parser.add_argument("--contacts", default="contacts/微信标签联系人.csv", help="wechat-labels 生成的标签联系人 CSV")
    reactivation_parser.add_argument("--handoffs", default="contacts/交接关系.csv", help="负责人交接关系 CSV；用于合并新负责人和共同讨论组")
    reactivation_parser.add_argument("--label", action="append", help="要分析的微信标签；默认读取个人 Profile 的复联标签")
    reactivation_parser.add_argument("--since", default="2025-11-01", help="只分析这个时间之后的聊天")
    reactivation_parser.add_argument("--until", help="索引时只拉取这个时间之前的聊天")
    reactivation_parser.add_argument("--inactive-days", type=int, default=21, help="超过多少天没互动才进入复联判断")
    reactivation_parser.add_argument("--self-name", action="append", help="补充你的微信发送者名字，可重复；默认读取 Profile")
    reactivation_parser.add_argument("--target-limit", type=int, default=300, help="最多分析多少个联系人")
    reactivation_parser.add_argument("--per-chat-limit", type=int, default=500, help="每个联系人最多读取/分析多少条消息")
    reactivation_parser.add_argument("--exclude-list", default=default_exclude_list, help="排除名单 txt；只在 --index-first 时使用")
    reactivation_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    reactivation_parser.add_argument("--index-first", action="store_true", help="先重新索引选定标签联系人，再分析")
    reactivation_parser.add_argument("--out", default="output/reactivation", help="输出目录")
    reactivation_parser.set_defaults(func=reactivation)

    group_parser = subparsers.add_parser("group-daily", help="总结每日活跃群聊，并提取商单/变现机会")
    group_parser.add_argument("--wechat-cli", default=DEFAULT_WECHAT_READER, help="只读 Reader 路径")
    group_parser.add_argument("--since", help="只总结这个时间之后的群聊消息；不填则默认过去 24 小时")
    group_parser.add_argument("--until", help="只总结这个时间之前的群聊消息")
    group_parser.add_argument("--hours", type=float, default=24, help="未指定 --since 时查看最近多少小时，默认 24")
    group_parser.add_argument("--group-limit", type=int, default=60, help="最多读取多少个最近活跃群聊")
    group_parser.add_argument("--per-group-limit", type=int, default=500, help="每个群最多读取多少条时间范围内消息")
    group_parser.add_argument("--min-link-chats", type=int, default=2, help="链接至少出现在多少个群才算跨群重复；带变现信号的链接会保留")
    group_parser.add_argument("--out", default="output/group-daily", help="输出目录")
    group_parser.add_argument("--exclude-list", default=default_exclude_list, help="排除名单 txt，匹配群名/摘要即跳过")
    group_parser.add_argument("--joined-channel-list", default=default_joined_channel_list, help="已加入渠道标记 txt；重复入群/扫码通知不再算新机会")
    group_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    group_parser.add_argument("--no-db", action="store_true", help="只生成文件，不写入本地情报库")
    group_parser.add_argument("--html", action="store_true", help="额外生成机器初筛 HTML；最终 HTML 应从语义编辑后的 Markdown 按需转换")
    group_parser.set_defaults(func=group_daily)

    render_report_parser = subparsers.add_parser("render-report", help="把最终 Markdown 报告按需转换成独立 HTML")
    render_report_parser.add_argument("source", help="已经完成语义编辑的 Markdown 报告")
    render_report_parser.add_argument("--out", help="HTML 输出路径；默认与 Markdown 同名")
    render_report_parser.add_argument("--css", help="自定义 CSS；默认使用 assets/report.css")
    render_report_parser.add_argument("--title", help="覆盖 HTML 页面标题")
    render_report_parser.set_defaults(func=render_report_command)

    render_bundle_parser = subparsers.add_parser(
        "render-bundle",
        help="把一次运行目录拆成分区 Markdown，并生成包含全部内容的可搜索旗舰 HTML",
    )
    render_bundle_parser.add_argument("report_dir", help="包含群聊、私信和总览报告的运行目录")
    render_bundle_parser.add_argument("--out", help="综合 HTML 输出路径；默认写入运行目录")
    render_bundle_parser.add_argument("--markdown-out", help="Markdown 报告入口路径；完整分区写入运行目录/wechat-report")
    render_bundle_parser.add_argument("--title", help="覆盖综合报告标题")
    render_bundle_parser.set_defaults(func=render_bundle_command)

    history_parser = subparsers.add_parser("chat-history", aliases=["view-chat"], help="查看任意联系人或群聊的最近聊天记录")
    history_parser.add_argument("chat", help="联系人备注、昵称、微信号或群名")
    history_parser.add_argument("--wechat-cli", default=DEFAULT_WECHAT_READER, help="只读 Reader 路径")
    history_parser.add_argument("--type-filter", choices=["all", "private", "group", "official_account"], default="all", help="限定聊天类型")
    history_parser.add_argument("--since", help="只查看这个时间之后的消息")
    history_parser.add_argument("--until", help="只查看这个时间之前的消息")
    history_parser.add_argument("--limit", type=int, default=200, help="最多读取多少条")
    history_parser.add_argument("--query", help="在该聊天记录里做简单关键词过滤")
    history_parser.add_argument("--out", default="output/chat-history", help="输出目录")
    history_parser.set_defaults(func=chat_history)

    common_groups_parser = subparsers.add_parser("common-groups", help="按微信成员 ID 精确查找多个联系人的共同群，并索引相关群聊")
    common_groups_parser.add_argument("contact", nargs="+", help="两个或多个联系人备注、昵称或微信号")
    common_groups_parser.add_argument("--wechat-cli", default=DEFAULT_WECHAT_READER, help="只读 Reader 路径")
    common_groups_parser.add_argument("--since", default="2025-11-01", help="只读取这个时间之后的共同群消息")
    common_groups_parser.add_argument("--until", help="只读取这个时间之前的共同群消息")
    common_groups_parser.add_argument("--group-limit", type=int, default=5000, help="最多扫描多少个群会话")
    common_groups_parser.add_argument("--member-limit", type=int, default=2000, help="每个群最多读取多少名成员")
    common_groups_parser.add_argument("--workers", type=int, default=8, help="并发检查群成员的线程数")
    common_groups_parser.add_argument("--per-group-limit", type=int, default=500, help="每个命中群最多读取多少条消息")
    common_groups_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    common_groups_parser.add_argument("--no-db", action="store_true", help="只生成文件，不写入本地情报库")
    common_groups_parser.add_argument("--out", default="output/common-groups", help="输出目录")
    common_groups_parser.set_defaults(func=common_groups)

    chat_search_parser = subparsers.add_parser("chat-search", aliases=["wechat-search", "search-all"], help="按关键词搜索全微信聊天记录并写入本地情报库")
    chat_search_parser.add_argument("query", help="要搜索的关键词")
    chat_search_parser.add_argument("--wechat-cli", default=DEFAULT_WECHAT_READER, help="只读 Reader 路径")
    chat_search_parser.add_argument("--chat", help="只搜索某个联系人或群聊")
    chat_search_parser.add_argument("--since", help="只搜索这个时间之后")
    chat_search_parser.add_argument("--until", help="只搜索这个时间之前")
    chat_search_parser.add_argument("--limit", type=int, default=100, help="每页搜索结果数")
    chat_search_parser.add_argument("--max-pages", type=int, default=3, help="最多翻几页")
    chat_search_parser.add_argument("--max-text-chars", type=int, default=1000, help="每条消息最多保留多少字符")
    chat_search_parser.add_argument("--exclude-list", default=default_exclude_list, help="排除名单 txt")
    chat_search_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    chat_search_parser.add_argument("--no-db", action="store_true", help="只生成文件，不写入本地情报库")
    chat_search_parser.add_argument("--out", default="output/chat-search", help="输出目录")
    chat_search_parser.set_defaults(func=chat_search)

    index_parser = subparsers.add_parser("db-index", help="把微信重点好友/最近会话/关键词搜索结果索引进本地情报库")
    index_parser.add_argument("--wechat-cli", default=DEFAULT_WECHAT_READER, help="只读 Reader 路径")
    index_parser.add_argument("--scope", choices=["labels", "sessions", "search"], default="labels", help="labels=商单/自媒体标签好友；sessions=最近会话；search=全微信关键词搜索")
    index_parser.add_argument("--contacts", default="contacts/微信标签联系人.csv", help="wechat-labels 生成的标签联系人 CSV")
    index_parser.add_argument("--label", action="append", help="scope=labels 时要索引的标签；默认读取个人 Profile 的重点标签")
    index_parser.add_argument("--target-limit", type=int, default=300, help="scope=labels 时最多索引多少个联系人")
    index_parser.add_argument("--session-type", choices=["all", "private", "group", "private,group"], default="all", help="scope=sessions 时索引哪类会话")
    index_parser.add_argument("--session-limit", type=int, default=80, help="scope=sessions 时读取多少个最近会话")
    index_parser.add_argument("--per-chat-limit", type=int, default=300, help="每个联系人/会话最多拉取多少条消息")
    index_parser.add_argument("--keyword", action="append", help="scope=search 时搜索关键词，可重复；不填则使用商单关键词包")
    index_parser.add_argument("--chat", help="scope=search 时只搜索某个聊天")
    index_parser.add_argument("--search-limit", type=int, default=100, help="scope=search 时每页搜索结果数")
    index_parser.add_argument("--max-pages", type=int, default=3, help="scope=search 时每个关键词最多翻几页")
    index_parser.add_argument("--max-text-chars", type=int, default=1000, help="scope=search 时每条消息最多保留多少字符")
    index_parser.add_argument("--since", default="2025-11-01", help="只索引这个时间之后")
    index_parser.add_argument("--until", help="只索引这个时间之前")
    index_parser.add_argument("--default-24h", action="store_true", help="忽略默认 since，改用过去 24 小时")
    index_parser.add_argument("--exclude-list", default=default_exclude_list, help="排除名单 txt")
    index_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    index_parser.add_argument("--out", default="output/db-index", help="索引输出目录")
    index_parser.set_defaults(func=db_index)

    search_parser = subparsers.add_parser("db-search", help="搜索本地微信情报库")
    search_parser.add_argument("query", help="FTS 搜索词，例如 商单 OR invoice")
    search_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    search_parser.add_argument("--chat", help="只搜索某个群聊名")
    search_parser.add_argument("--since", help="只搜索这个时间之后")
    search_parser.add_argument("--until", help="只搜索这个时间之前")
    search_parser.add_argument("--limit", type=int, default=30, help="最多返回多少条")
    search_parser.add_argument("--out", help="把搜索结果写到 Markdown 文件")
    search_parser.set_defaults(func=db_search)

    db_status_parser = subparsers.add_parser("db-status", help="查看本地微信情报库状态")
    db_status_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    db_status_parser.set_defaults(func=db_status)

    cleanup_parser = subparsers.add_parser("cleanup", help="预览或清理历史输出文件；默认不删除")
    cleanup_parser.add_argument("--root", default="output", help="要清理的 output 目录")
    cleanup_parser.add_argument("--raw-days", type=int, default=7, help="JSON/JSONL/raw 文件保留天数")
    cleanup_parser.add_argument("--report-days", type=int, default=30, help="Markdown/CSV/日志等报告保留天数")
    cleanup_parser.add_argument("--apply", action="store_true", help="确认执行删除；不加时只预览")
    cleanup_parser.set_defaults(func=cleanup_command)

    home_parser = subparsers.add_parser("home", help="查看微信个人情报库五个入口和当前状态")
    home_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地微信情报库 SQLite 路径")
    home_parser.add_argument("--out", help="输出 Markdown 文件")
    home_parser.set_defaults(func=home_command)

    person_parser = subparsers.add_parser("person", help="汇总一个联系人的关系上下文、承诺、要求和商机")
    person_parser.add_argument("person", help="联系人或会话名；模糊匹配不唯一时会要求补全")
    person_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地微信情报库 SQLite 路径")
    person_parser.add_argument("--since", help="只分析这个时间之后的消息")
    person_parser.add_argument("--until", help="只刷新/分析这个时间之前的消息")
    person_parser.add_argument("--limit", type=int, default=500, help="最多分析最近多少条消息，默认 500")
    person_parser.add_argument("--refresh", action="store_true", help="先从微信实时刷新该联系人，再生成报告")
    person_parser.add_argument("--wechat-cli", default=DEFAULT_WECHAT_READER, help="只读 Reader 路径；只在 --refresh 时使用")
    person_parser.add_argument("--type", dest="type_filter", choices=["user", "group"], help="实时刷新时限定私聊或群聊")
    person_parser.add_argument("--self-name", action="append", help="补充你的微信发送者名字，可重复；默认读取 Profile")
    person_parser.add_argument("--out", help="输出 Markdown 文件")
    person_parser.set_defaults(func=person_command)

    reply_parser = subparsers.add_parser("reply", help="根据联系人上下文生成只读回复建议，不发送微信")
    reply_parser.add_argument("person", help="联系人或会话名；模糊匹配不唯一时会要求补全")
    reply_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地微信情报库 SQLite 路径")
    reply_parser.add_argument("--limit", type=int, default=120, help="最多分析最近多少条消息，默认 120")
    reply_parser.add_argument("--style-days", type=int, help="覆盖 Profile 中的本人私聊风格窗口天数")
    reply_parser.add_argument("--minimum-chat-messages", type=int, help="覆盖 Profile 中启用联系人专属口语的最少本人消息数")
    reply_parser.add_argument("--self-name", action="append", help="补充你的微信发送者名字，可重复；默认读取 Profile")
    reply_parser.add_argument("--out", help="输出 Markdown 文件")
    reply_parser.set_defaults(func=reply_command)

    topic_parser = subparsers.add_parser("topic", help="跨私聊和群聊聚合一个主题的近期情报")
    topic_parser.add_argument("topic", help="主题，例如 培训、赚钱、商单、结算")
    topic_parser.add_argument("--keyword", action="append", help="补充关键词，可重复")
    topic_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地微信情报库 SQLite 路径")
    topic_parser.add_argument("--since", help="明确指定时间起点；提供后覆盖 --days")
    topic_parser.add_argument("--days", type=int, default=7, help="默认查看最近几天，默认 7")
    topic_parser.add_argument("--limit-messages", type=int, default=800, help="最多读取多少条命中消息")
    topic_parser.add_argument("--limit-chats", type=int, default=20, help="最多展开多少个会话")
    topic_parser.add_argument("--out", help="输出 Markdown 文件")
    topic_parser.set_defaults(func=topic_command)

    brief_parser = subparsers.add_parser("brief", help="生成微信个人情报简报，并与前一等长窗口比较")
    brief_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地微信情报库 SQLite 路径")
    brief_parser.add_argument("--hours", type=int, default=24, help="当前窗口小时数，默认 24")
    brief_parser.add_argument("--limit-chats", type=int, default=10, help="私聊和群聊各最多展开多少个")
    brief_parser.add_argument("--self-name", action="append", help="补充你的微信发送者名字，可重复；默认读取 Profile")
    brief_parser.add_argument("--out", help="输出 Markdown 文件")
    brief_parser.set_defaults(func=brief_command)

    contact_daily_parser = subparsers.add_parser(
        "contact-daily",
        aliases=["private-daily"],
        help="按个人 Profile 总结重点标签联系人、合作对象和自定义主题私聊，并给出回复方向",
    )
    contact_daily_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地微信情报库 SQLite 路径")
    contact_daily_parser.add_argument("--hours", type=int, default=24, help="未指定 since 时查看最近多少小时，默认 24")
    contact_daily_parser.add_argument("--since", help="明确指定时间起点；提供后覆盖 --hours")
    contact_daily_parser.add_argument("--until", help="时间终点；默认当前时间")
    contact_daily_parser.add_argument("--contacts", default="contacts/微信标签联系人.csv", help="wechat-labels 生成的标签联系人 CSV")
    contact_daily_parser.add_argument("--self-name", action="append", help="补充你的微信发送者名字，可重复；默认读取 Profile")
    contact_daily_parser.add_argument("--limit", type=int, default=80, help="最多输出多少个联系人，默认 80")
    contact_daily_parser.add_argument("--out", default="output/contact-daily", help="输出目录")
    contact_daily_parser.set_defaults(func=contact_daily_command)

    today_parser = subparsers.add_parser("today", help="查看今天最值得处理的微信商机，默认最多 10 条")
    today_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地微信情报库 SQLite 路径")
    today_parser.add_argument("--min-priority", type=int, choices=range(0, 6), default=3, help="最低优先级，默认 3")
    today_parser.add_argument("--limit", type=int, default=10, help="最多返回多少条，默认 10")
    today_parser.add_argument("--out", help="输出 Markdown 文件")
    today_parser.set_defaults(func=today_command)

    inbox_parser = subparsers.add_parser("inbox", help="查看尚未人工分流的高优先级情报")
    inbox_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地微信情报库 SQLite 路径")
    inbox_parser.add_argument("--min-priority", type=int, choices=range(0, 6), default=4, help="最低优先级，默认 4")
    inbox_parser.add_argument("--limit", type=int, default=20, help="最多返回多少条，默认 20")
    inbox_parser.add_argument("--out", help="输出 Markdown 文件")
    inbox_parser.set_defaults(func=inbox_command)

    triage_parser = subparsers.add_parser("triage", help="把一条待分流情报确认为推进、等待、暂缓或关闭")
    triage_parser.add_argument("id", type=int, help="商机 ID")
    triage_parser.add_argument("decision", choices=sorted(TRIAGE_DECISIONS), help="pursue/wait/pause/ignore/won/lost")
    triage_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地微信情报库 SQLite 路径")
    triage_parser.add_argument("--stage", choices=STAGE_ORDER, help="同时设置商单阶段并锁定人工判断")
    triage_parser.add_argument("--priority", type=int, choices=range(0, 6), help="同时设置 0-5 优先级")
    triage_parser.add_argument("--next-action", help="同时设置下一步动作")
    triage_parser.add_argument("--follow-up", help="下次跟进日期；wait 时必填")
    triage_parser.add_argument("--note", help="追加一条内部备注")
    triage_parser.set_defaults(func=triage_command)

    opportunities_parser = subparsers.add_parser("opportunities", help="查看持久化微信商机管线")
    opportunities_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    opportunities_parser.add_argument("--status", choices=sorted(ALL_STATUSES), help="只查看指定状态")
    opportunities_parser.add_argument("--stage", choices=STAGE_ORDER, help="只查看指定商单阶段")
    opportunities_parser.add_argument("--chat", help="按联系人或群聊名筛选")
    opportunities_parser.add_argument("--due-only", action="store_true", help="只查看已经到跟进日期的商机")
    opportunities_parser.add_argument("--include-closed", action="store_true", help="包含已成交、未成交和已忽略记录")
    opportunities_parser.add_argument(
        "--include-candidates",
        action="store_true",
        help="同时显示尚未人工确认的候选；默认只看正式机会",
    )
    opportunities_parser.add_argument("--min-priority", type=int, choices=range(0, 6), default=3, help="最低优先级，默认 3；设为 0 查看全部")
    opportunities_parser.add_argument("--limit", type=int, default=50, help="最多返回多少个商机")
    opportunities_parser.add_argument("--out", help="输出 Markdown 文件")
    opportunities_parser.set_defaults(func=opportunities_command)

    opportunity_maintain_parser = subparsers.add_parser(
        "opportunity-maintain",
        help="预览或标记长期没有新信号的过期候选",
    )
    opportunity_maintain_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    opportunity_maintain_parser.add_argument("--stale-days", type=int, default=14, help="候选多少天无新信号后过期，默认 14")
    opportunity_maintain_parser.add_argument("--apply", action="store_true", help="执行过期标记；默认仅预览")
    opportunity_maintain_parser.set_defaults(func=opportunity_maintain_command)

    opportunity_sync_parser = subparsers.add_parser("opportunity-sync", help="从已有微信情报库回填商机管线")
    opportunity_sync_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地微信情报库 SQLite 路径")
    opportunity_sync_parser.add_argument("--since", help="只回填这个时间之后的消息")
    opportunity_sync_parser.add_argument("--until", help="只回填这个时间之前的消息")
    opportunity_sync_parser.add_argument("--chat", help="只回填指定联系人或群聊")
    opportunity_sync_parser.add_argument("--exclude-list", default=default_exclude_list, help="排除名单 txt")
    opportunity_sync_parser.add_argument("--dry-run", action="store_true", help="只预览候选，不写入商机表")
    opportunity_sync_parser.set_defaults(func=opportunity_sync_command)

    opportunity_update_parser = subparsers.add_parser("opportunity-update", help="更新商机阶段、状态和跟进计划")
    opportunity_update_parser.add_argument("id", type=int, help="商机 ID")
    opportunity_update_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    opportunity_update_parser.add_argument("--stage", choices=STAGE_ORDER, help="设置商单阶段并锁定人工判断")
    opportunity_update_parser.add_argument("--status", choices=sorted(ALL_STATUSES), help="设置推进状态")
    opportunity_update_parser.add_argument("--priority", type=int, choices=range(0, 6), help="设置 0-5 优先级")
    opportunity_update_parser.add_argument("--next-action", help="设置下一步动作并锁定人工判断")
    opportunity_update_parser.add_argument("--follow-up", help="设置下次跟进日期，例如 2026-08-03")
    opportunity_update_parser.add_argument("--clear-follow-up", action="store_true", help="清除跟进日期")
    opportunity_update_parser.add_argument("--note", help="追加一条内部备注")
    opportunity_update_parser.add_argument("--unlock-stage", action="store_true", help="允许后续扫描自动更新阶段")
    opportunity_update_parser.add_argument("--unlock-priority", action="store_true", help="允许后续扫描自动更新优先级")
    opportunity_update_parser.add_argument("--unlock-next-action", action="store_true", help="允许后续扫描自动更新下一步")
    opportunity_update_parser.set_defaults(func=opportunity_update_command)

    feedback_add_parser = subparsers.add_parser("feedback-add", help="记录误报、确认机会或低优先级反馈")
    feedback_add_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    feedback_add_parser.add_argument("--target-type", choices=["chat", "opportunity", "message", "link"], required=True)
    feedback_add_parser.add_argument("--target", required=True, help="对象值；chat 建议使用完整聊天名")
    feedback_add_parser.add_argument("--verdict", choices=sorted(FEEDBACK_VERDICTS), required=True)
    feedback_add_parser.add_argument("--note", help="反馈原因")
    feedback_add_parser.set_defaults(func=feedback_add_command)

    feedback_list_parser = subparsers.add_parser("feedback-list", help="查看历史人工反馈")
    feedback_list_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    feedback_list_parser.add_argument("--target-type", choices=["chat", "opportunity", "message", "link"])
    feedback_list_parser.add_argument("--limit", type=int, default=50)
    feedback_list_parser.set_defaults(func=feedback_list_command)

    links_parser = subparsers.add_parser("db-links", help="从本地情报库聚合跨群重复链接/商单链接")
    links_parser.add_argument("--db", default=DEFAULT_RADAR_DB, help="本地情报库 SQLite 路径")
    links_parser.add_argument("--since", help="只统计这个时间之后")
    links_parser.add_argument("--until", help="只统计这个时间之前")
    links_parser.add_argument("--chat", help="只统计某个群聊名")
    links_parser.add_argument("--min-chats", type=int, default=2, help="至少覆盖多少个群")
    links_parser.add_argument("--limit", type=int, default=50, help="最多输出多少个链接")
    links_parser.add_argument("--out", default="output/db-cross-group-links.md", help="输出 Markdown 文件")
    links_parser.set_defaults(func=db_links)
    return parser


def main() -> None:
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args()
    configure_profile(args.profile)
    args.func(args)


if __name__ == "__main__":
    main()

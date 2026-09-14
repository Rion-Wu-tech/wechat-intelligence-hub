from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import re
import sqlite3
from typing import Any, Iterable


COMMERCIAL_TERMS = re.compile(
    r"合作|商单|推广|投放|品牌|报价|预算|brief|排期|发布|审核|结算|付款|"
    r"培训|讲师|授课|工作坊|咨询|项目|招募|佣金|返佣|campaign|sponsor|invoice|payment",
    re.I,
)
REQUEST_TERMS = re.compile(
    r"请问|麻烦|方便|能否|可以吗|什么时候|确认一下|发我|给我|回复|"
    r"报价|预算|价位|价格|费用|多少钱|怎么收费|多少(?:呢|钱)?|大致.{0,8}(?:价|费用)|brief|排期",
    re.I,
)
PROMISE_TERMS = re.compile(
    r"我(?:会|来|可以|今晚|明天|之后|稍后|回头).{0,32}"
    r"(?:整理|确认|回复|发|给|做|改|补|推进|安排|加热|转发|quote|联系|提交|发布|完成|跟进|回传)|"
    r"(?:整理好|写好|改好|确认后).{0,16}(?:发你|给你|回复你)|"
    r"(?:今天|明天|后天|周[一二三四五六日天]|本周|下周).{0,24}"
    r"(?:给|发|提交|发布|完成|安排|回传)|(?:可以|能).{0,12}(?:给|发|提交).{0,12}(?:初稿|二稿|终稿|方案|数据)",
    re.I,
)
ATTACHMENT_TERMS = re.compile(r"^\[(?:图片|文件|语音|视频|image|file|voice|video)\]$", re.I)
PROJECT_CHAT_NAME_TERMS = re.compile(r"未结|初稿|终稿|对接群|交付群|合作群|项目群", re.I)
POST_PUBLISH_ACTION_TERMS = re.compile(
    r"浏览量.{0,16}(?:低|少|只有)|增加.{0,16}曝光|补(?:量|曝光|互动)|"
    r"找.{0,16}(?:KOL|博主|达人).{0,16}(?:转发|加热)|"
    r"(?:KOL|博主|达人).{0,16}(?:转发|加热)|多\s*quote|"
    r"数据统计.{0,16}(?:发布后|天|截止)|数据回传|回传数据",
    re.I,
)
PROMISE_COMPLETION_TERMS = re.compile(
    r"(?:已经|已)(?:完成|安排|提交|发送|发给|发布|联系|转发|加热|回传)|"
    r"(?:完成|安排|提交|发送|发布|联系|转发|加热|回传)(?:了|好啦|好了)|"
    r"(?:初稿|二稿|终稿|数据|链接).{0,12}(?:发你|给你|已发|提交)",
    re.I,
)

TOPIC_EXPANSIONS = {
    "培训": ["培训", "讲师", "授课", "工作坊", "课程", "教练"],
    "赚钱": ["赚钱", "变现", "收入", "报价", "预算", "佣金", "付费", "项目合作"],
    "商单": ["商单", "品牌合作", "推广", "投放", "campaign", "sponsor", "brief"],
    "结算": ["结算", "付款", "打款", "到账", "发票", "invoice", "payment"],
}


def shorten(value: str, limit: int = 220) -> str:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def normalize_since(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().replace("/", "-")
    if len(value) == 10:
        return value + " 00:00:00"
    return value


def is_self_sender(sender: str, self_names: Iterable[str]) -> bool:
    folded = sender.strip().casefold()
    return any(folded == name.strip().casefold() or folded.startswith(name.strip().casefold()) for name in self_names)


def known_group_chats(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "select distinct chat from messages where source_file like '%@chatroom%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def is_project_collaboration_chat(
    chat: str,
    rows: list[dict[str, Any]],
    groups: set[str],
    self_names: Iterable[str] = ("我", "me", "自己"),
) -> bool:
    """Recognize small delivery rooms that should behave like direct deal chats."""
    if chat not in groups and not any("@chatroom" in str(row.get("source_file") or "") for row in rows):
        return False
    senders = {
        str(row.get("sender") or "").strip()
        for row in rows
        if str(row.get("sender") or "").strip() not in {"", "系统"}
    }
    has_commercial_context = any(
        COMMERCIAL_TERMS.search(str(row.get("content") or ""))
        or POST_PUBLISH_ACTION_TERMS.search(str(row.get("content") or ""))
        for row in rows
    )
    owner_aliases = [name.strip() for name in self_names if name.strip().casefold() not in {"我", "me", "自己"}]
    named_for_delivery = bool(PROJECT_CHAT_NAME_TERMS.search(chat)) or any(
        alias.casefold() in chat.casefold() for alias in owner_aliases
    )
    directly_addresses_owner = any(
        re.search(rf"(?:@\s*)?{re.escape(alias)}\s*(?:老师)?", str(row.get("content") or ""), re.I)
        for alias in owner_aliases
        for row in rows
    )
    return has_commercial_context and len(senders) <= 12 and (named_for_delivery or directly_addresses_owner)


def infer_promise_action(content: str) -> str:
    if POST_PUBLISH_ACTION_TERMS.search(content):
        return "落实补量、Quote/KOL 转发，并在统计截止前回传新增数据"
    if re.search(r"初稿|二稿|终稿|草稿", content, re.I):
        return "按承诺时间完成并提交稿件"
    if re.search(r"发布|上线", content, re.I):
        return "确认发布排期并按时上线"
    if re.search(r"联系|转发|加热|quote", content, re.I):
        return "完成已承诺的联系、转发或加热动作"
    if re.search(r"数据|回传", content, re.I):
        return "整理并回传约定数据"
    return "完成这项承诺并向对方同步结果"


def resolve_chat_name(conn: sqlite3.Connection, query: str) -> str:
    rows = conn.execute(
        """
        select chat, count(*) as message_count, max(time) as last_time
        from messages
        where lower(chat) like ?
        group by chat
        order by last_time desc
        limit 12
        """,
        (f"%{query.strip().casefold()}%",),
    ).fetchall()
    if not rows:
        raise ValueError(f"情报库里找不到联系人或会话：{query}")
    exact = [str(row["chat"]) for row in rows if str(row["chat"]).casefold() == query.strip().casefold()]
    if exact:
        return exact[0]
    if len(rows) == 1:
        return str(rows[0]["chat"])
    candidates = "、".join(str(row["chat"]) for row in rows[:8])
    raise ValueError(f"“{query}”匹配多个会话，请使用更完整的名字：{candidates}")


def home_report(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    current = now or datetime.now()
    today = current.strftime("%Y-%m-%d")
    recent_since = (current - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    total_messages = int(conn.execute("select count(*) from messages").fetchone()[0])
    latest_message = str(conn.execute("select max(time) from messages").fetchone()[0] or "")
    open_opportunities = int(
        conn.execute(
            "select count(*) from opportunities where status in ('active', 'waiting', 'paused') and priority >= 4"
        ).fetchone()[0]
    )
    due_opportunities = int(
        conn.execute(
            """
            select count(*) from opportunities
            where status in ('active', 'waiting', 'paused')
              and next_follow_up is not null
              and next_follow_up != ''
              and substr(next_follow_up, 1, 10) <= ?
            """,
            (today,),
        ).fetchone()[0]
    )
    inbox_count = int(
        conn.execute(
            """
            select count(*) from opportunities
            where status = 'new' and priority >= 4 and last_signal_time >= ?
            """,
            (recent_since,),
        ).fetchone()[0]
    )

    freshness = "暂无索引"
    if latest_message:
        try:
            latest_dt = datetime.fromisoformat(latest_message)
            comparison_current = current
            if latest_dt.tzinfo is not None and comparison_current.tzinfo is None:
                comparison_current = comparison_current.replace(tzinfo=datetime.now().astimezone().tzinfo)
            elif latest_dt.tzinfo is None and comparison_current.tzinfo is not None:
                latest_dt = latest_dt.replace(tzinfo=comparison_current.tzinfo)
            age = max(0.0, (comparison_current - latest_dt).total_seconds() / 3600)
            freshness = f"约 {age:.1f} 小时前"
        except ValueError:
            freshness = latest_message

    lines = [
        "# 微信个人情报库｜入口",
        "",
        f"- 最新索引：{latest_message or '无'}（{freshness}）",
        f"- 本地消息：{total_messages} 条",
        f"- 高优先级推进中：{open_opportunities} 个；今日到期：{due_opportunities} 个；近 24 小时待分流：{inbox_count} 个",
        "",
        "## 五个入口",
        "",
        "1. **今日总览**：看近 24 小时变化、待回复、重点私聊和群聊。",
        "   `hub.sh brief --hours 24`",
        "2. **主题搜索**：跨群聊和私聊找一个主题，不受日报日期限制。",
        "   `hub.sh topic \"主题\" --days 7`",
        "3. **联系人**：查看双方要求、个人承诺、开放商机和最近上下文。",
        "   `hub.sh person \"联系人\" --refresh`",
        "4. **回复建议**：基于最近消息和商机状态生成草稿，必须人工确认后自行发送。",
        "   `hub.sh reply \"联系人\"`",
        "5. **商单雷达**：查看今日行动、待分流候选和完整商机管线。",
        "   `hub.sh today` / `hub.sh inbox` / `hub.sh opportunities`",
        "",
        "## 信息分流",
        "",
        f"- **立即处理**：{due_opportunities} 个已到期跟进；优先运行 `today`。",
        f"- **值得关注**：近 24 小时有 {inbox_count} 个高优先级待分流候选；运行 `inbox`。",
        f"- **仅供存档**：其余消息保留在本地索引，可通过 `topic`、`person` 或 `db-search` 按需检索。",
    ]
    metadata = {
        "messages": total_messages,
        "latest_message": latest_message,
        "open_opportunities": open_opportunities,
        "due_opportunities": due_opportunities,
        "inbox": inbox_count,
    }
    return "\n".join(lines) + "\n", metadata


def _reply_intent(latest_content: str, stage: str) -> str:
    if re.search(r"结算|付款|打款|发票|invoice|payment", latest_content, re.I):
        return "settlement"
    if re.search(r"审核|修改|反馈|review", latest_content, re.I):
        return "review"
    if re.search(r"报价|预算|费用|价格|quote|rate", latest_content, re.I):
        return "quote"
    if re.search(r"brief|需求|要求|交付|素材", latest_content, re.I):
        return "brief"
    if re.search(r"排期|发布|什么时候|日期|提交|交稿|初稿|二稿|终稿|deadline", latest_content, re.I):
        return "schedule"
    if len(latest_content.strip()) <= 8:
        if re.search(r"结算|付款|发票", stage, re.I):
            return "settlement"
        if re.search(r"审核|修改", stage, re.I):
            return "review"
        if re.search(r"报价|预算", stage, re.I):
            return "quote"
    return "general"


REPLY_CLOSE_TERMS = re.compile(
    r"(?:bro|bros?|hhh?)|兄弟|哥们|宝宝|宝贝|好滴|好叭|捏|哈哈|～|~|\[(?:旺柴|破涕为笑|社会社会|狗头|裂开)\]",
    re.I,
)
REPLY_PROFESSIONAL_TERMS = re.compile(
    r"老师|您好|辛苦|合作|报价|预算|brief|交付|排期|审核|结算|付款|发票|项目",
    re.I,
)
REPLY_CLOSURE_TERMS = re.compile(
    r"^(?:好|好的|好滴|行|可以|收到|明白|了解|ok|嗯+|谢谢|辛苦|哈哈+|hhh+|\[(?:表情|图片)\])[。！!～~ ]*$",
    re.I,
)
REPLY_ADVICE_TERMS = re.compile(
    r"建议|我觉得|最好|可以.{0,12}(?:精简|优化|调整)|对你.{0,12}(?:有用|有帮助|比较好)",
    re.I,
)


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _preferred_style_token(texts: list[str], patterns: list[tuple[str, re.Pattern[str]]]) -> str:
    hits: list[str] = []
    for text in texts:
        for label, pattern in patterns:
            if pattern.search(text):
                hits.append(label)
    if not hits:
        return ""
    counts = Counter(hits)
    last_seen = {token: max(index for index, value in enumerate(hits) if value == token) for token in counts}
    return max(counts, key=lambda token: (counts[token], last_seen[token]))


def _summarize_reply_style(texts: list[str]) -> dict[str, Any]:
    clean = [text.strip() for text in texts if text.strip()]
    lengths = [len(text) for text in clean]
    total = len(clean)
    ack = _preferred_style_token(
        clean,
        [
            ("好滴", re.compile(r"(?:^|[，,。\s])好滴(?:$|[呀啊哈～~！!，,。\s])", re.I)),
            ("好嘞", re.compile(r"(?:^|[，,。\s])好嘞(?:$|[呀啊哈～~！!，,。\s])", re.I)),
            ("好的", re.compile(r"(?:^|[，,。\s])好的(?:$|[呀啊哈～~！!，,。\s])", re.I)),
            ("收到", re.compile(r"(?:^|[，,。\s])收到(?:$|[呀啊哈～~！!，,。\s])", re.I)),
            ("ok", re.compile(r"(?:^|\s)ok(?:$|[呀啊哈～~！!，,。\s])", re.I)),
            ("嗯嗯", re.compile(r"(?:^|[，,。\s])嗯嗯(?:$|[呀啊哈～~！!，,。\s])")),
            ("行", re.compile(r"(?:^|[，,。\s])行(?:$|[呀啊哈～~！!，,。\s])")),
        ],
    )
    laugh = _preferred_style_token(
        clean,
        [
            ("hhh", re.compile(r"h{3,}", re.I)),
            ("hh", re.compile(r"(?<!h)hh(?!h)", re.I)),
            ("哈哈哈", re.compile(r"哈哈哈+")),
            ("哈哈", re.compile(r"(?<!哈)哈哈(?!哈)")),
        ],
    )
    return {
        "messages": total,
        "p80_chars": _percentile(lengths, 0.80),
        "pct_le20": round(100 * sum(length <= 20 for length in lengths) / total, 1) if total else 0.0,
        "pct_multiline": round(100 * sum("\n" in text for text in clean) / total, 1) if total else 0.0,
        "pct_final_period": round(100 * sum(text.endswith(("。", ".")) for text in clean) / total, 1) if total else 0.0,
        "preferred_ack": ack,
        "preferred_laugh": laugh,
    }


def _global_reply_style(
    conn: sqlite3.Connection,
    self_names: Iterable[str],
    *,
    days: int = 30,
) -> dict[str, Any]:
    """Summarize recent private-message shape without exposing raw chat text."""
    latest = str(conn.execute("select max(time) from messages").fetchone()[0] or "")
    if not latest:
        return {"messages": 0, "chats": 0, "p80_chars": 0, "pct_le20": 0.0, "pct_multiline": 0.0}
    try:
        anchor = datetime.fromisoformat(latest)
    except ValueError:
        anchor = datetime.now()
    since = (anchor - timedelta(days=max(1, days))).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """
        select chat, sender, content, source_file
        from messages
        where time >= ? and source_file not like '%@chatroom%'
        """,
        (since,),
    ).fetchall()
    texts: list[str] = []
    chats: set[str] = set()
    for row in rows:
        if not is_self_sender(str(row["sender"]), self_names):
            continue
        content = str(row["content"] or "").strip()
        if (
            not content
            or ATTACHMENT_TERMS.match(content)
            or re.fullmatch(r"\[[^\]]+\]", content)
            or content.startswith("http")
            or len(content) > 500
        ):
            continue
        texts.append(content)
        chats.add(str(row["chat"]))
    summary = _summarize_reply_style(texts)
    summary["chats"] = len(chats)
    return summary


def _current_chat_reply_style(
    messages: list[dict[str, Any]],
    self_names: Iterable[str],
) -> dict[str, Any]:
    texts = [
        str(row.get("content") or "")
        for row in messages
        if is_self_sender(str(row.get("sender") or ""), self_names)
        and not ATTACHMENT_TERMS.match(str(row.get("content") or "").strip())
        and not re.fullmatch(r"\[[^\]]+\]", str(row.get("content") or "").strip())
        and not str(row.get("content") or "").strip().startswith("http")
        and len(str(row.get("content") or "")) <= 500
    ]
    return _summarize_reply_style(texts[-80:])


def _reply_relationship(messages: list[dict[str, Any]], self_names: Iterable[str], stage: str) -> str:
    self_texts = [
        str(row.get("content") or "")
        for row in messages
        if is_self_sender(str(row.get("sender") or ""), self_names)
    ]
    other_texts = [
        str(row.get("content") or "")
        for row in messages
        if str(row.get("sender") or "") != "系统"
        and not is_self_sender(str(row.get("sender") or ""), self_names)
    ]
    own = " ".join(self_texts[-80:])
    mutual = " ".join([*self_texts[-40:], *other_texts[-40:]])
    close = bool(REPLY_CLOSE_TERMS.search(mutual))
    professional = bool(REPLY_PROFESSIONAL_TERMS.search(f"{own} {stage}"))
    if close and professional:
        return "熟悉的合作对象"
    if close:
        return "亲近朋友"
    if professional:
        return "商务联系人"
    if len(messages) < 12:
        return "新联系人"
    return "熟人/同辈"


def _reply_draft(
    intent: str,
    latest_content: str,
    relationship: str,
    chat_style: dict[str, Any],
    minimum_chat_messages: int = 5,
) -> str:
    close = relationship in {"亲近朋友", "熟悉的合作对象"}
    enough_style = int(chat_style.get("messages") or 0) >= max(1, minimum_chat_messages)
    ack = str(chat_style.get("preferred_ack") or "") if enough_style else ""
    laugh = str(chat_style.get("preferred_laugh") or "") if enough_style and close else ""
    casual_ack = ack if ack in {"好滴", "好嘞", "ok", "嗯嗯", "行"} else "收到"
    neutral_ack = ack if ack in {"好的", "收到", "ok"} else "收到"
    if REPLY_ADVICE_TERMS.search(latest_content):
        suffix = laugh or "哈哈"
        return f"有道理，我后面尽量说人话点{suffix}" if close else "有道理，我后面会再精简一点"
    drafts = {
        "settlement": "我核对下结算信息，确认后跟你说" if close else "收到，我核对下结算信息，确认后回复你",
        "review": f"{casual_ack}，我按反馈改，完成后发你" if close else f"{neutral_ack}，我按反馈修改，完成后发你审核",
        "quote": "对，前面是单条价。批量的话可以按数量重算，先问下大概每月几条就行" if close else "前面是单条定制价。批量可以按数量重算，麻烦先确认大概每月几条",
        "brief": f"{casual_ack}，我先看下，缺什么我一起问你" if close else f"{neutral_ack}，我先核对需求，缺的信息我集中确认",
        "schedule": "我看下现在的进度，确认后给你具体时间" if close else "我先核对进度，确认后给你具体时间",
        "general": "看到了，我确认下再跟你说" if close else "收到，我确认后回复你",
    }
    return drafts[intent]


def reply_report(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 120,
    self_names: Iterable[str] = ("我", "me", "自己"),
    style_days: int = 30,
    minimum_chat_messages: int = 5,
) -> tuple[str, dict[str, Any]]:
    self_names = tuple(self_names)
    chat = resolve_chat_name(conn, query)
    rows = [
        dict(row)
        for row in conn.execute(
            """
            select chat, sender, time, content, source_file
            from messages where chat = ?
            order by time desc limit ?
            """,
            (chat, max(1, limit)),
        ).fetchall()
    ]
    messages = list(reversed(rows))
    conversation = [row for row in messages if str(row.get("sender") or "") != "系统"]
    incoming = [row for row in conversation if not is_self_sender(str(row["sender"]), self_names)]
    latest_incoming = incoming[-1] if incoming else None
    latest_overall = conversation[-1] if conversation else None
    promises = [
        row for row in messages
        if is_self_sender(str(row["sender"]), self_names) and PROMISE_TERMS.search(str(row["content"]))
    ]
    opportunity_row = conn.execute(
        """
        select id, title, stage, status, priority, next_action, next_follow_up
        from opportunities
        where chat = ? and status in ('new', 'active', 'waiting', 'paused')
        order by priority desc, last_signal_time desc limit 1
        """,
        (chat,),
    ).fetchone()
    opportunity = dict(opportunity_row) if opportunity_row else None
    stage = str(opportunity.get("stage") or "") if opportunity else ""
    latest_content = str(latest_incoming["content"]) if latest_incoming else ""
    intent = _reply_intent(latest_content, stage)
    relationship = _reply_relationship(messages, self_names, stage)
    style = _global_reply_style(conn, self_names, days=style_days)
    chat_style = _current_chat_reply_style(messages, self_names)
    owner_sent_last = bool(
        latest_overall and is_self_sender(str(latest_overall.get("sender") or ""), self_names)
    )
    closure = bool(REPLY_CLOSURE_TERMS.match(latest_content.strip()))
    reply_needed = bool(latest_incoming) and not owner_sent_last and not closure
    recommended = (
        _reply_draft(
            intent,
            latest_content,
            relationship,
            chat_style,
            minimum_chat_messages=minimum_chat_messages,
        )
        if reply_needed
        else ""
    )

    lines = [
        f"# 微信回复建议：{chat}",
        "",
        "> 只生成本地草稿，不会发送、转发或操作微信。发送前核对事实、金额、日期和承诺。",
    ]
    if owner_sent_last:
        lines.extend(["", "## 建议", "", "不用再回。你已经发过消息，等对方下一条。"])
    elif closure:
        lines.extend(["", "## 建议", "", "不用特意回；熟人可以补一个表情。"])
    elif recommended:
        lines.extend(["", "## 建议发", "", f"> {recommended}"])
    else:
        lines.extend(["", "## 建议", "", "暂时没有需要回复的消息。"])
    lines.extend(
        [
            "",
            "## 判断",
            "",
            f"- {relationship} · {intent} · {'需要回复' if reply_needed else '无需追发'}",
            f"- 最近对方：{latest_incoming['time'] if latest_incoming else '未找到'}｜{shorten(latest_content, 120) if latest_content else '无'}",
            f"- 近 {style_days} 天个人习惯：{style['pct_le20']}% 的私聊不超过 20 字；默认只给一条短回复。",
        ]
    )
    if opportunity:
        lines.append(f"- 商机：#{opportunity['id']}｜{stage}｜{str(opportunity.get('next_action') or '待确认')}")
    if promises and reply_needed:
        lines.append(f"- 注意已有承诺：{shorten(promises[-1]['content'], 100)}")
    metadata = {
        "chat": chat,
        "messages": len(messages),
        "intent": intent,
        "relationship": relationship,
        "reply_needed": reply_needed,
        "owner_sent_last": owner_sent_last,
        "style": style,
        "chat_style": chat_style,
        "has_incoming": bool(latest_incoming),
        "has_opportunity": bool(opportunity),
    }
    return "\n".join(lines) + "\n", metadata


def person_report(
    conn: sqlite3.Connection,
    query: str,
    *,
    since: str | None = None,
    limit: int = 500,
    self_names: Iterable[str] = ("我", "me", "自己"),
) -> tuple[str, dict[str, Any]]:
    chat = resolve_chat_name(conn, query)
    params: list[Any] = [chat]
    where = "chat = ?"
    if since:
        where += " and time >= ?"
        params.append(normalize_since(since))
    total = int(conn.execute(f"select count(*) from messages where {where}", params).fetchone()[0])
    rows = conn.execute(
        f"""
        select chat, sender, time, content, source_file
        from messages
        where {where}
        order by time desc
        limit ?
        """,
        [*params, max(1, limit)],
    ).fetchall()
    messages = [dict(row) for row in reversed(rows)]
    opportunities = [
        dict(row)
        for row in conn.execute(
            """
            select id, title, status, stage, priority, amount, last_signal_time,
                   next_action, next_follow_up, notes
            from opportunities
            where chat = ? and status in ('new', 'active', 'waiting', 'paused')
            order by priority desc, last_signal_time desc
            limit 10
            """,
            (chat,),
        ).fetchall()
    ]
    latest = messages[-1] if messages else None
    self_promises = [
        row for row in messages
        if is_self_sender(str(row["sender"]), self_names) and PROMISE_TERMS.search(str(row["content"]))
    ][-5:]
    partner_requests = [
        row for row in messages
        if not is_self_sender(str(row["sender"]), self_names) and REQUEST_TERMS.search(str(row["content"]))
    ][-5:]
    commercial = [row for row in messages if COMMERCIAL_TERMS.search(str(row["content"]))][-8:]

    direction = "未知"
    if latest:
        direction = "我最后发出" if is_self_sender(str(latest["sender"]), self_names) else "对方最后发来"
    next_action = "人工查看最近上下文"
    if latest and direction == "对方最后发来":
        next_action = "先回复对方最后一条消息"
        if opportunities and opportunities[0].get("next_action"):
            next_action += f"；商机系统建议：{opportunities[0]['next_action']}"
    elif opportunities and opportunities[0].get("next_action"):
        next_action = str(opportunities[0]["next_action"])

    lines = [
        f"# 微信联系人情报：{chat}",
        "",
        f"- 已索引消息：{total} 条；本次分析最近 {len(messages)} 条",
        f"- 时间范围：{messages[0]['time'] if messages else '无'} 至 {messages[-1]['time'] if messages else '无'}",
        f"- 最后消息方向：{direction}",
        f"- 开放商机：{len(opportunities)} 个",
        f"- 当前建议：{next_action}",
        "",
        "## 开放商机",
        "",
    ]
    if not opportunities:
        lines.append("暂无已确认的开放商机。")
    for row in opportunities:
        follow_up = f"；跟进 {row['next_follow_up']}" if row["next_follow_up"] else ""
        amount = f"；金额 {row['amount']}" if row["amount"] else ""
        lines.append(
            f"- **#{row['id']}｜{row['stage']}｜优先级 {row['priority']}**："
            f"{row['next_action'] or '待人工确认'}{follow_up}{amount}"
        )

    lines.extend(["", "## 我答应过的事项", ""])
    if not self_promises:
        lines.append("未检出明确承诺；仍需结合上下文人工确认。")
    for row in reversed(self_promises):
        lines.append(f"- **{row['time']}**：{shorten(row['content'])}")

    lines.extend(["", "## 对方近期要求", ""])
    if not partner_requests:
        lines.append("未检出明确要求。")
    for row in reversed(partner_requests):
        lines.append(f"- **{row['time']}｜{row['sender']}**：{shorten(row['content'])}")

    lines.extend(["", "## 商业相关时间线", ""])
    if not commercial:
        lines.append("暂无明显商业信号。")
    for row in commercial:
        lines.append(f"- **{row['time']}｜{row['sender']}**：{shorten(row['content'])}")

    lines.extend(["", "## 最近上下文", ""])
    for row in messages[-12:]:
        lines.append(f"- **{row['time']}｜{row['sender']}**：{shorten(row['content'])}")
    return "\n".join(lines) + "\n", {"chat": chat, "messages": len(messages), "opportunities": len(opportunities)}


def expand_topic_terms(topic: str, extra_keywords: Iterable[str] = ()) -> list[str]:
    terms: list[str] = []
    for value in [topic, *extra_keywords]:
        clean = value.strip()
        if not clean:
            continue
        expanded = TOPIC_EXPANSIONS.get(clean, [clean])
        for term in expanded:
            if term.casefold() not in {item.casefold() for item in terms}:
                terms.append(term)
    return terms


def topic_report(
    conn: sqlite3.Connection,
    topic: str,
    *,
    extra_keywords: Iterable[str] = (),
    since: str | None = None,
    limit_messages: int = 800,
    limit_chats: int = 20,
) -> tuple[str, dict[str, Any]]:
    terms = expand_topic_terms(topic, extra_keywords)
    clauses = ["lower(content) like ?" for _ in terms]
    params: list[Any] = [f"%{term.casefold()}%" for term in terms]
    sql = f"""
        select chat, sender, time, content, source_file
        from messages
        where ({' or '.join(clauses)})
    """
    if since:
        sql += " and time >= ?"
        params.append(normalize_since(since))
    sql += " order by time desc limit ?"
    params.append(max(1, limit_messages))
    messages = [dict(row) for row in conn.execute(sql, params).fetchall()]
    groups = known_group_chats(conn)
    by_chat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in messages:
        by_chat[str(row["chat"])].append(row)

    ranked = sorted(
        by_chat.items(),
        key=lambda item: (
            sum(bool(COMMERCIAL_TERMS.search(str(row["content"]))) for row in item[1]),
            len(item[1]),
            max(str(row["time"]) for row in item[1]),
        ),
        reverse=True,
    )[: max(1, limit_chats)]
    lines = [
        f"# 微信主题情报：{topic}",
        "",
        f"- 检索词：{'、'.join(terms)}",
        f"- 时间起点：{normalize_since(since) or '不限'}",
        f"- 命中消息：{len(messages)} 条；涉及会话：{len(by_chat)} 个",
        "",
        "## 重点会话",
        "",
    ]
    if not ranked:
        lines.append("暂无命中。")
    for chat, rows in ranked:
        rows = sorted(rows, key=lambda row: str(row["time"]))
        actionable = sum(bool(COMMERCIAL_TERMS.search(str(row["content"]))) for row in rows)
        chat_type = "群聊" if chat in groups or any("@chatroom" in str(row["source_file"]) for row in rows) else "私聊"
        lines.append(
            f"### {chat}｜{chat_type}｜{len(rows)} 条｜商业相关 {actionable} 条｜最后 {rows[-1]['time']}"
        )
        lines.append("")
        for row in rows[-3:]:
            lines.append(f"- **{row['time']}｜{row['sender']}**：{shorten(row['content'])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", {"messages": len(messages), "chats": len(by_chat), "terms": terms}


def _window_messages(conn: sqlite3.Connection, since: str, until: str, limit: int = 12000) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            select chat, sender, time, content, source_file
            from messages
            where time >= ? and time < ?
            order by time asc
            limit ?
            """,
            (since, until, limit),
        ).fetchall()
    ]


def brief_report(
    conn: sqlite3.Connection,
    *,
    hours: int = 24,
    limit_chats: int = 10,
    now: datetime | None = None,
    self_names: Iterable[str] = ("我", "me", "自己"),
) -> tuple[str, dict[str, Any]]:
    current = now or datetime.now()
    since_dt = current - timedelta(hours=max(1, hours))
    previous_since_dt = since_dt - timedelta(hours=max(1, hours))
    fmt = "%Y-%m-%d %H:%M:%S"
    current_rows = _window_messages(conn, since_dt.strftime(fmt), current.strftime(fmt))
    previous_rows = _window_messages(conn, previous_since_dt.strftime(fmt), since_dt.strftime(fmt))
    groups = known_group_chats(conn)

    def chat_count(rows: list[dict[str, Any]]) -> int:
        return len({str(row["chat"]) for row in rows})

    latest_db_time = str(conn.execute("select max(time) from messages").fetchone()[0] or "")
    freshness_hours: float | None = None
    if latest_db_time:
        try:
            parsed_db_time = datetime.fromisoformat(latest_db_time)
            if parsed_db_time.tzinfo is None and current.tzinfo is not None:
                parsed_db_time = parsed_db_time.astimezone()
            elif parsed_db_time.tzinfo is not None and current.tzinfo is None:
                parsed_db_time = parsed_db_time.replace(tzinfo=None)
            freshness_hours = max(0.0, (current - parsed_db_time).total_seconds() / 3600)
        except (ValueError, TypeError):
            pass

    by_chat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in current_rows:
        by_chat[str(row["chat"])].append(row)

    def is_group(chat: str, rows: list[dict[str, Any]]) -> bool:
        return chat in groups or any("@chatroom" in str(row["source_file"]) for row in rows)

    ranked = sorted(
        by_chat.items(),
        key=lambda item: (
            sum(bool(COMMERCIAL_TERMS.search(str(row["content"]))) for row in item[1]),
            len(item[1]),
            max(str(row["time"]) for row in item[1]),
        ),
        reverse=True,
    )
    direct_ranked_all = [
        (chat, rows)
        for chat, rows in ranked
        if not is_group(chat, rows) or is_project_collaboration_chat(chat, rows, groups, self_names)
    ]
    private_ranked = direct_ranked_all[:limit_chats]
    group_ranked = [
        (chat, rows)
        for chat, rows in ranked
        if is_group(chat, rows) and not is_project_collaboration_chat(chat, rows, groups, self_names)
    ][:limit_chats]

    pending_replies: list[tuple[str, dict[str, Any]]] = []
    for chat, rows in direct_ranked_all:
        latest = sorted(rows, key=lambda row: str(row["time"]))[-1]
        if not is_self_sender(str(latest["sender"]), self_names) and REQUEST_TERMS.search(str(latest["content"])):
            pending_replies.append((chat, latest))

    open_promises: list[tuple[str, dict[str, Any], str]] = []
    for chat, rows in direct_ranked_all:
        ordered = sorted(rows, key=lambda row: str(row["time"]))
        promise_indexes = [
            index
            for index, row in enumerate(ordered)
            if is_self_sender(str(row["sender"]), self_names)
            and PROMISE_TERMS.search(str(row["content"]))
        ]
        if not promise_indexes:
            continue
        promise_index = promise_indexes[-1]
        later_self_messages = [
            row
            for row in ordered[promise_index + 1 :]
            if is_self_sender(str(row["sender"]), self_names)
        ]
        if any(PROMISE_COMPLETION_TERMS.search(str(row["content"])) for row in later_self_messages):
            continue
        promise = ordered[promise_index]
        open_promises.append((chat, promise, infer_promise_action(str(promise["content"]))))
    open_promises.sort(key=lambda item: str(item[1]["time"]), reverse=True)

    tracked_opportunities = [
        dict(row)
        for row in conn.execute(
            """
            select id, chat, title, opportunity_type, stage, status, priority,
                   amount, last_signal_time, next_action, next_follow_up
            from opportunities
            where status in ('active', 'waiting', 'paused')
              and priority >= 3
              and last_signal_time >= ?
            order by priority desc, last_signal_time desc
            limit 10
            """,
            (since_dt.strftime(fmt),),
        ).fetchall()
    ]
    candidate_opportunities = [
        dict(row)
        for row in conn.execute(
            """
            select id, chat, title, opportunity_type, stage, status, priority,
                   amount, last_signal_time, next_action, next_follow_up
            from opportunities
            where status = 'new'
              and priority >= 4
              and last_signal_time >= ?
            order by priority desc, last_signal_time desc
            limit 5
            """,
            (since_dt.strftime(fmt),),
        ).fetchall()
    ]
    attachments = [row for row in current_rows if ATTACHMENT_TERMS.search(str(row["content"]).strip())]
    topic_counts = Counter()
    for row in current_rows:
        content = str(row["content"])
        for name, terms in TOPIC_EXPANSIONS.items():
            if any(term.casefold() in content.casefold() for term in terms):
                topic_counts[name] += 1

    stale_note = ""
    if freshness_hours is None:
        stale_note = "无法判断数据新鲜度。"
    elif freshness_hours > 2:
        stale_note = f"⚠️ 最新索引距现在约 {freshness_hours:.1f} 小时，涉及‘现在/最新’的判断应先刷新。"
    else:
        stale_note = f"最新索引距现在约 {freshness_hours:.1f} 小时。"

    current_chat_count = chat_count(current_rows)
    previous_chat_count = chat_count(previous_rows)
    chat_coverage_ratio = max(current_chat_count, previous_chat_count) / max(
        1, min(current_chat_count, previous_chat_count)
    )
    message_coverage_ratio = max(len(current_rows), len(previous_rows)) / max(
        1, min(len(current_rows), len(previous_rows))
    )
    coverage_comparable = bool(current_rows and previous_rows) and (
        chat_coverage_ratio <= 3 and message_coverage_ratio <= 10
    )

    if freshness_hours is not None and freshness_hours > 2:
        change_line = (
            f"- 变化：当前窗口尚未完整刷新，暂不解读环比；"
            f"原始计数为消息 {len(current_rows) - len(previous_rows):+d}、"
            f"会话 {chat_count(current_rows) - chat_count(previous_rows):+d}"
        )
    elif not coverage_comparable:
        change_line = (
            "- 变化：两个窗口的会话/消息覆盖差异过大，暂不解读环比；"
            f"原始计数为消息 {len(current_rows) - len(previous_rows):+d}、"
            f"会话 {current_chat_count - previous_chat_count:+d}"
        )
    else:
        change_line = (
            f"- 变化：消息 {len(current_rows) - len(previous_rows):+d}；"
            f"会话 {chat_count(current_rows) - chat_count(previous_rows):+d}"
        )

    lines = [
        f"# 微信个人情报简报｜近 {hours} 小时",
        "",
        f"- 当前窗口：{since_dt.strftime(fmt)} 至 {current.strftime(fmt)}",
        f"- 消息：{len(current_rows)} 条；会话：{chat_count(current_rows)} 个",
        f"- 前一窗口：{len(previous_rows)} 条；会话：{chat_count(previous_rows)} 个",
        change_line,
        f"- 数据新鲜度：{stale_note}",
        "",
        "## 待回复",
        "",
    ]
    if not pending_replies:
        lines.append("未检出明确待回复私聊。")
    for chat, row in pending_replies[:10]:
        lines.append(f"- **{chat}｜{row['time']}**：{shorten(row['content'])}")

    lines.extend(["", "## 待兑现承诺", ""])
    if not open_promises:
        lines.append("当前窗口未检出尚无完成证据的明确承诺。")
    for chat, row, action in open_promises[:10]:
        lines.append(
            f"- **{chat}｜{row['time']}**：{shorten(row['content'])}；下一步：{action}"
        )

    lines.extend(["", "## 已确认或正在推进", ""])
    if not tracked_opportunities:
        lines.append("当前窗口没有已人工分流且正在推进的商业机会。")
    for row in tracked_opportunities:
        follow_up = f"；跟进 {row['next_follow_up']}" if row["next_follow_up"] else ""
        lines.append(
            f"- **#{row['id']}｜{row['title'] or row['chat']}**：{row['opportunity_type']} / "
            f"{row['stage']} / 优先级 {row['priority']}；{row['next_action'] or '待人工确认'}{follow_up}"
        )

    lines.extend(["", "## 待审核候选", ""])
    if not candidate_opportunities:
        lines.append("当前窗口没有达到审核门槛的新候选。")
    else:
        lines.append("> 以下内容尚未确认，不等于真实商单；确认后才进入正式合作管线。")
    for row in candidate_opportunities:
        lines.append(
            f"- **#{row['id']}｜{row['title'] or row['chat']}**：{row['opportunity_type']} / "
            f"优先级 {row['priority']}；{row['next_action'] or '需要人工判断'}"
        )

    lines.extend(["", "## 重点私聊", ""])
    if not private_ranked:
        lines.append("当前窗口暂无私聊。")
    for chat, rows in private_ranked[:6]:
        rows = sorted(rows, key=lambda row: str(row["time"]))
        commercial_count = sum(bool(COMMERCIAL_TERMS.search(str(row["content"]))) for row in rows)
        lines.append(f"- **{chat}**：{len(rows)} 条，商业相关 {commercial_count} 条；最后：{shorten(rows[-1]['content'])}")

    lines.extend(["", "## 重点群聊", ""])
    if not group_ranked:
        lines.append("当前窗口暂无群聊。")
    for chat, rows in group_ranked[:6]:
        rows = sorted(rows, key=lambda row: str(row["time"]))
        commercial_count = sum(bool(COMMERCIAL_TERMS.search(str(row["content"]))) for row in rows)
        lines.append(f"- **{chat}**：{len(rows)} 条，商业相关 {commercial_count} 条；最后：{shorten(rows[-1]['content'])}")

    lines.extend(["", "## 主题变化", ""])
    if not topic_counts:
        lines.append("暂无预设主题命中。")
    if topic_counts:
        lines.append("- " + "；".join(f"{name} {count} 条" for name, count in topic_counts.most_common(6)))

    lines.extend(["", "## 附件核验队列", ""])
    unique_attachments: list[dict[str, Any]] = []
    seen_attachment_chats: set[str] = set()
    for row in reversed(attachments):
        chat = str(row["chat"])
        if chat in seen_attachment_chats:
            continue
        seen_attachment_chats.add(chat)
        unique_attachments.append(row)
        if len(unique_attachments) >= 5:
            break
    if not unique_attachments:
        lines.append("当前窗口没有待核验附件占位。")
    elif len(attachments) > len(unique_attachments):
        lines.append(f"> 共 {len(attachments)} 个附件占位；这里只列最近的 {len(unique_attachments)} 个会话。")
    for row in reversed(unique_attachments):
        lines.append(f"- **{row['time']}｜{row['chat']}｜{row['sender']}**：{row['content']}")

    metadata = {
        "messages": len(current_rows),
        "chats": chat_count(current_rows),
        "previous_messages": len(previous_rows),
        "opportunities": len(tracked_opportunities),
        "candidates": len(candidate_opportunities),
        "pending_replies": len(pending_replies),
        "open_promises": len(open_promises),
        "freshness_hours": freshness_hours,
        "coverage_comparable": coverage_comparable,
    }
    return "\n".join(lines) + "\n", metadata

from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3
from typing import Any, Iterable


OPEN_STATUSES = {"new", "active", "waiting", "paused"}
CLOSED_STATUSES = {"won", "lost"}
INACTIVE_STATUSES = {"ignored", "stale", "archived"}
ALL_STATUSES = OPEN_STATUSES | CLOSED_STATUSES | INACTIVE_STATUSES
RECORD_TYPES = {"candidate", "opportunity"}
CONFIDENCE_LEVELS = {"low", "medium", "high", "confirmed"}
FEEDBACK_VERDICTS = {"confirmed", "false_positive", "ignore", "low_priority"}
TRIAGE_DECISIONS = {
    "pursue": "active",
    "wait": "waiting",
    "pause": "paused",
    "ignore": "ignored",
    "won": "won",
    "lost": "lost",
}

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


def init_opportunity_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists opportunities (
            id integer primary key autoincrement,
            opportunity_key text not null default '',
            chat text not null,
            title text not null,
            opportunity_type text not null default '其他合作',
            stage text not null default '新线索',
            status text not null default 'new',
            priority integer not null default 0,
            amount text not null default '',
            last_signal_time text not null default '',
            next_action text not null default '',
            next_follow_up text not null default '',
            notes text not null default '',
            source_run_id integer,
            stage_locked integer not null default 0,
            priority_locked integer not null default 0,
            next_action_locked integer not null default 0,
            created_at text not null,
            updated_at text not null,
            closed_at text not null default '',
            record_type text not null default 'candidate',
            confidence text not null default 'medium',
            qualification_score integer not null default 0,
            qualification_reasons text not null default '',
            reinforcement_count integer not null default 1,
            expires_at text not null default '',
            last_reviewed_at text not null default '',
            foreign key(source_run_id) references runs(id)
        );

        create index if not exists idx_opportunities_chat
        on opportunities(chat);

        create index if not exists idx_opportunities_status
        on opportunities(status);

        create index if not exists idx_opportunities_follow_up
        on opportunities(next_follow_up);

        create table if not exists opportunity_evidence (
            opportunity_id integer not null,
            message_id integer not null,
            created_at text not null,
            unique(opportunity_id, message_id),
            foreign key(opportunity_id) references opportunities(id) on delete cascade,
            foreign key(message_id) references messages(id) on delete cascade
        );

        create table if not exists opportunity_feedback (
            id integer primary key autoincrement,
            target_type text not null,
            target_key text not null,
            verdict text not null,
            note text not null default '',
            created_at text not null
        );

        create index if not exists idx_opportunity_feedback_target
        on opportunity_feedback(target_type, target_key, id);
        """
    )
    columns = {
        str(row["name"])
        for row in conn.execute("pragma table_info(opportunities)").fetchall()
    }
    if "opportunity_key" not in columns:
        conn.execute(
            "alter table opportunities add column opportunity_key text not null default ''"
        )
        conn.execute(
            "update opportunities set opportunity_key = 'legacy:' || id where opportunity_key = ''"
        )
    migrations = {
        "record_type": "text not null default 'candidate'",
        "confidence": "text not null default 'medium'",
        "qualification_score": "integer not null default 0",
        "qualification_reasons": "text not null default ''",
        "reinforcement_count": "integer not null default 1",
        "expires_at": "text not null default ''",
        "last_reviewed_at": "text not null default ''",
    }
    for column, declaration in migrations.items():
        if column not in columns:
            conn.execute(f"alter table opportunities add column {column} {declaration}")
    # Human decisions always make a record a formal opportunity. A stage that only
    # the extractor inferred does not: that legacy rule runs once, when a database
    # from before record_type existed is upgraded, and never on later opens.
    legacy_stage_rule = "" if "record_type" in columns else "or stage != '新线索'"
    conn.execute(
        f"""
        update opportunities
        set record_type = 'opportunity'
        where record_type = 'candidate'
          and (
                status in ('active', 'waiting', 'paused', 'won', 'lost')
                or stage_locked = 1
                {legacy_stage_rule}
          )
        """
    )
    conn.execute(
        "create index if not exists idx_opportunities_key on opportunities(opportunity_key)"
    )
    conn.execute(
        "create index if not exists idx_opportunities_record_type on opportunities(record_type, status)"
    )
    conn.execute(
        "create index if not exists idx_opportunities_expires on opportunities(expires_at)"
    )


def _stage_rank(stage: str) -> int:
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return 0


def _split_amounts(value: str) -> list[str]:
    return [item.strip() for item in value.split(" / ") if item.strip()]


def _merge_values(existing: str, incoming: str) -> str:
    merged: list[str] = []
    for value in [*_split_amounts(existing), *_split_amounts(incoming)]:
        if value not in merged:
            merged.append(value)
    return " / ".join(merged)


def _latest_feedback(conn: sqlite3.Connection, target_type: str, target_key: str) -> str:
    row = conn.execute(
        """
        select verdict
        from opportunity_feedback
        where target_type = ? and target_key = ?
        order by id desc
        limit 1
        """,
        (target_type, target_key),
    ).fetchone()
    return str(row["verdict"]) if row else ""


def _candidate_expiry(candidate_time: str, created_at: str, days: int = 14) -> str:
    value = candidate_time or created_at
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = datetime.fromisoformat(created_at)
    return (parsed + timedelta(days=max(1, days))).strftime("%Y-%m-%d %H:%M:%S")


def expire_stale_candidates(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    stale_days: int = 14,
    apply: bool = True,
) -> list[dict[str, Any]]:
    now_value = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cutoff = (datetime.fromisoformat(now_value) - timedelta(days=max(1, stale_days))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = conn.execute(
        """
        select *
        from opportunities
        where record_type = 'candidate'
          and status = 'new'
          and next_follow_up = ''
          and last_reviewed_at = ''
          and (
                (expires_at != '' and expires_at <= ?)
                or (expires_at = '' and last_signal_time != '' and last_signal_time <= ?)
          )
        order by last_signal_time asc, id asc
        """,
        (now_value, cutoff),
    ).fetchall()
    if apply and rows:
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"update opportunities set status = 'stale', updated_at = ? where id in ({placeholders})",
            (now_value, *ids),
        )
    return [dict(row) for row in rows]


def _attach_evidence(
    conn: sqlite3.Connection,
    opportunity_id: int,
    message_hashes: Iterable[str],
    created_at: str,
) -> None:
    for digest in dict.fromkeys(message_hashes):
        row = conn.execute("select id from messages where hash = ?", (digest,)).fetchone()
        if not row:
            continue
        conn.execute(
            """
            insert or ignore into opportunity_evidence(opportunity_id, message_id, created_at)
            values (?, ?, ?)
            """,
            (opportunity_id, int(row["id"]), created_at),
        )


def sync_opportunity_candidates(
    conn: sqlite3.Connection,
    run_id: int,
    candidates: list[dict[str, Any]],
    created_at: str,
) -> tuple[int, int, int]:
    created = 0
    updated = 0
    skipped = 0

    expire_stale_candidates(conn, now=created_at, apply=True)

    for candidate in candidates:
        chat = str(candidate.get("chat") or "").strip()
        opportunity_key = str(candidate.get("opportunity_key") or "").strip()
        if not chat or not opportunity_key:
            skipped += 1
            continue

        verdict = _latest_feedback(conn, "opportunity", opportunity_key) or _latest_feedback(
            conn, "chat", chat
        )
        if verdict in {"false_positive", "ignore"}:
            skipped += 1
            continue

        priority = int(candidate.get("priority") or 0)
        if verdict == "low_priority":
            priority = min(priority, 1)
        elif verdict == "confirmed":
            priority = max(priority, 5)

        record_type = str(candidate.get("record_type") or "candidate")
        if verdict == "confirmed":
            record_type = "opportunity"
        if record_type not in RECORD_TYPES:
            record_type = "candidate"
        confidence = str(candidate.get("confidence") or "medium")
        if verdict == "confirmed":
            confidence = "confirmed"
        if confidence not in CONFIDENCE_LEVELS:
            confidence = "medium"
        qualification_score = max(0, min(100, int(candidate.get("qualification_score") or 0)))
        qualification_reasons = str(candidate.get("qualification_reasons") or "")

        latest = conn.execute(
            """
            select *
            from opportunities
            where opportunity_key = ?
            order by id desc
            limit 1
            """,
            (opportunity_key,),
        ).fetchone()

        if latest is None and candidate.get("project_collaboration"):
            legacy = conn.execute(
                """
                select *
                from opportunities
                where chat = ?
                  and opportunity_key like 'group:%'
                  and status in ('new', 'active', 'waiting', 'paused')
                order by last_signal_time desc, id desc
                limit 1
                """,
                (chat,),
            ).fetchone()
            if legacy is not None:
                conn.execute(
                    "update opportunities set opportunity_key = ?, updated_at = ? where id = ?",
                    (opportunity_key, created_at, int(legacy["id"])),
                )
                latest = conn.execute(
                    "select * from opportunities where id = ?",
                    (int(legacy["id"]),),
                ).fetchone()

        candidate_time = str(candidate.get("last_signal_time") or "")
        create_new = latest is None
        if latest and str(latest["status"]) in CLOSED_STATUSES:
            create_new = candidate_time > str(latest["last_signal_time"] or "")
        if latest and str(latest["status"]) == "ignored":
            skipped += 1
            continue

        if create_new:
            cursor = conn.execute(
                """
                insert into opportunities(
                    opportunity_key, chat, title, opportunity_type, stage, status, priority, amount,
                    last_signal_time, next_action, source_run_id, created_at, updated_at,
                    record_type, confidence, qualification_score, qualification_reasons,
                    reinforcement_count, expires_at
                )
                values (?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    opportunity_key,
                    chat,
                    str(candidate.get("title") or chat),
                    str(candidate.get("opportunity_type") or "其他合作"),
                    str(candidate.get("stage") or "新线索"),
                    priority,
                    str(candidate.get("amount") or ""),
                    candidate_time,
                    str(candidate.get("next_action") or ""),
                    run_id,
                    created_at,
                    created_at,
                    record_type,
                    confidence,
                    qualification_score,
                    qualification_reasons,
                    _candidate_expiry(candidate_time, created_at) if record_type == "candidate" else "",
                ),
            )
            opportunity_id = int(cursor.lastrowid)
            created += 1
        else:
            opportunity_id = int(latest["id"])
            existing_stage = str(latest["stage"])
            incoming_stage = str(candidate.get("stage") or "新线索")
            stage = existing_stage
            if not int(latest["stage_locked"]) and _stage_rank(incoming_stage) >= _stage_rank(existing_stage):
                stage = incoming_stage

            existing_priority = int(latest["priority"])
            final_priority = existing_priority
            if not int(latest["priority_locked"]):
                final_priority = max(existing_priority, priority)

            next_action = str(latest["next_action"])
            if not int(latest["next_action_locked"]) and (
                stage != existing_stage or not next_action
            ):
                next_action = str(candidate.get("next_action") or next_action)

            last_signal_time = max(str(latest["last_signal_time"] or ""), candidate_time)
            opportunity_type = str(latest["opportunity_type"])
            incoming_type = str(candidate.get("opportunity_type") or "")
            if incoming_type == "培训/咨询/项目合作" or not opportunity_type:
                opportunity_type = incoming_type or opportunity_type

            has_new_signal = candidate_time > str(latest["last_signal_time"] or "")
            final_record_type = str(latest["record_type"] or "candidate")
            if final_record_type == "candidate" and record_type == "opportunity":
                final_record_type = "opportunity"
            final_confidence = str(latest["confidence"] or "medium")
            confidence_rank = {"low": 0, "medium": 1, "high": 2, "confirmed": 3}
            if confidence_rank.get(confidence, 1) > confidence_rank.get(final_confidence, 1):
                final_confidence = confidence
            final_status = str(latest["status"])
            if final_status == "stale" and has_new_signal:
                final_status = "new"
            reinforcement_count = int(latest["reinforcement_count"] or 1) + int(has_new_signal)
            expires_at = str(latest["expires_at"] or "")
            if final_record_type == "opportunity":
                expires_at = ""
            elif has_new_signal or not expires_at:
                expires_at = _candidate_expiry(candidate_time, created_at)

            conn.execute(
                """
                update opportunities
                set opportunity_type = ?, stage = ?, priority = ?, amount = ?,
                    last_signal_time = ?, next_action = ?, source_run_id = ?, updated_at = ?,
                    record_type = ?, confidence = ?, qualification_score = max(qualification_score, ?),
                    qualification_reasons = case when ? != '' then ? else qualification_reasons end,
                    reinforcement_count = ?, expires_at = ?, status = ?
                where id = ?
                """,
                (
                    opportunity_type,
                    stage,
                    final_priority,
                    (
                        str(candidate.get("amount") or "")
                        if candidate.get("replace_amount")
                        else _merge_values(str(latest["amount"]), str(candidate.get("amount") or ""))
                    ),
                    last_signal_time,
                    next_action,
                    run_id,
                    created_at,
                    final_record_type,
                    final_confidence,
                    qualification_score,
                    qualification_reasons,
                    qualification_reasons,
                    reinforcement_count,
                    expires_at,
                    final_status,
                    opportunity_id,
                ),
            )
            updated += 1

        _attach_evidence(
            conn,
            opportunity_id,
            [str(value) for value in candidate.get("message_hashes") or []],
            created_at,
        )

    return created, updated, skipped


def list_opportunities(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    stage: str | None = None,
    chat: str | None = None,
    due_only: bool = False,
    include_closed: bool = False,
    min_priority: int = 0,
    record_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    sql = """
        select o.*,
               (select count(*) from opportunity_evidence e where e.opportunity_id = o.id) as evidence_count
        from opportunities o
        where 1 = 1
    """
    params: list[Any] = []
    if status:
        sql += " and o.status = ?"
        params.append(status)
    elif not include_closed:
        sql += " and o.status in ('new', 'active', 'waiting', 'paused')"
    if stage:
        sql += " and o.stage = ?"
        params.append(stage)
    if chat:
        sql += " and o.chat like ?"
        params.append(f"%{chat}%")
    if min_priority > 0:
        sql += " and o.priority >= ?"
        params.append(min_priority)
    if record_type:
        if record_type not in RECORD_TYPES:
            raise ValueError(f"不支持的记录类型：{record_type}")
        sql += " and o.record_type = ?"
        params.append(record_type)
    if due_only:
        sql += " and o.next_follow_up != '' and substr(o.next_follow_up, 1, 10) <= ?"
        params.append(datetime.now().strftime("%Y-%m-%d"))
    sql += """
        order by
            case when o.next_follow_up != '' then 0 else 1 end,
            o.next_follow_up asc,
            o.priority desc,
            o.last_signal_time desc
        limit ?
    """
    params.append(max(1, limit))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def list_opportunity_inbox(
    conn: sqlite3.Connection,
    *,
    min_priority: int = 4,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = list_opportunities(
        conn,
        status="new",
        min_priority=min_priority,
        limit=limit * 3,
    )
    rows.sort(
        key=lambda row: (
            {"confirmed": 3, "high": 2, "medium": 1, "low": 0}.get(
                str(row.get("confidence") or "medium"), 1
            ),
            int(row.get("reinforcement_count") or 1),
            int(row.get("priority") or 0),
            str(row.get("last_signal_time") or ""),
        ),
        reverse=True,
    )
    return rows[:limit]


def normalize_follow_up(value: str) -> str:
    """Accept YYYY-MM-DD with an optional time; reject anything a date comparison would misread."""
    text = str(value or "").strip()
    try:
        if len(text) == 10:
            return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
        parsed = datetime.fromisoformat(text.replace("T", " "))
    except ValueError:
        raise ValueError(f"跟进日期格式无效：{text or '空'}；请使用 YYYY-MM-DD，例如 2026-08-20") from None
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def list_today_opportunities(
    conn: sqlite3.Connection,
    *,
    today: str | None = None,
    min_priority: int = 3,
    limit: int = 10,
) -> list[dict[str, Any]]:
    today_value = (today or datetime.now().strftime("%Y-%m-%d"))[:10]
    # A waiting record without a date predates the date requirement; it stays
    # visible at the end of the list instead of silently dropping out.
    sql = """
        select o.*,
               (select count(*) from opportunity_evidence e where e.opportunity_id = o.id) as evidence_count
        from opportunities o
        where o.status in ('new', 'active', 'waiting')
          and o.priority >= ?
          and (o.record_type = 'opportunity' or o.confidence in ('high', 'confirmed'))
          and (
                (o.next_follow_up != '' and substr(o.next_follow_up, 1, 10) <= ?)
                or o.next_follow_up = ''
          )
        order by
            case
                when o.next_follow_up != '' and substr(o.next_follow_up, 1, 10) <= ? then 0
                when o.status = 'waiting' then 4
                when o.stage = '已发布待结算' then 1
                when o.status = 'active' then 2
                else 3
            end,
            o.priority desc,
            o.last_signal_time desc
        limit ?
    """
    return [
        dict(row)
        for row in conn.execute(
            sql,
            (min_priority, today_value, today_value, max(1, limit)),
        ).fetchall()
    ]


def update_opportunity(
    conn: sqlite3.Connection,
    opportunity_id: int,
    *,
    stage: str | None = None,
    status: str | None = None,
    priority: int | None = None,
    next_action: str | None = None,
    next_follow_up: str | None = None,
    note: str | None = None,
    clear_follow_up: bool = False,
    unlock_stage: bool = False,
    unlock_priority: bool = False,
    unlock_next_action: bool = False,
) -> dict[str, Any]:
    row = conn.execute("select * from opportunities where id = ?", (opportunity_id,)).fetchone()
    if not row:
        raise ValueError(f"找不到商机 ID：{opportunity_id}")
    if status and status not in ALL_STATUSES:
        raise ValueError(f"不支持的状态：{status}")
    if next_follow_up is not None and not clear_follow_up:
        next_follow_up = normalize_follow_up(next_follow_up)

    values = dict(row)
    final_status = status if status is not None else str(values.get("status") or "")
    final_follow_up = "" if clear_follow_up else (
        next_follow_up if next_follow_up is not None else str(values.get("next_follow_up") or "")
    )
    if final_status == "waiting" and not final_follow_up and (status == "waiting" or clear_follow_up):
        raise ValueError("等待中的商机必须有跟进日期：请同时提供 --follow-up YYYY-MM-DD，避免它从今日行动里消失")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    assignments: list[str] = ["updated_at = ?"]
    params: list[Any] = [now]

    def set_value(column: str, value: Any) -> None:
        assignments.append(f"{column} = ?")
        params.append(value)

    if stage is not None:
        set_value("stage", stage)
        set_value("stage_locked", 1)
    elif unlock_stage:
        set_value("stage_locked", 0)
    if status is not None:
        set_value("status", status)
        set_value("closed_at", now if status in CLOSED_STATUSES else "")
        if status in {"active", "waiting", "paused", "won", "lost"}:
            set_value("record_type", "opportunity")
            set_value("expires_at", "")
            set_value("last_reviewed_at", now)
        elif status in INACTIVE_STATUSES:
            set_value("last_reviewed_at", now)
    if priority is not None:
        set_value("priority", priority)
        set_value("priority_locked", 1)
    elif unlock_priority:
        set_value("priority_locked", 0)
    if next_action is not None:
        set_value("next_action", next_action)
        set_value("next_action_locked", 1)
    elif unlock_next_action:
        set_value("next_action_locked", 0)
    if clear_follow_up:
        set_value("next_follow_up", "")
    elif next_follow_up is not None:
        set_value("next_follow_up", next_follow_up)
    if note:
        existing_note = str(values.get("notes") or "")
        entry = f"[{now}] {note.strip()}"
        set_value("notes", f"{existing_note}\n{entry}".strip())

    params.append(opportunity_id)
    conn.execute(
        f"update opportunities set {', '.join(assignments)} where id = ?",
        params,
    )
    updated = conn.execute("select * from opportunities where id = ?", (opportunity_id,)).fetchone()
    return dict(updated)


def triage_opportunity(
    conn: sqlite3.Connection,
    opportunity_id: int,
    decision: str,
    *,
    next_follow_up: str | None = None,
    stage: str | None = None,
    priority: int | None = None,
    next_action: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    if decision not in TRIAGE_DECISIONS:
        raise ValueError(f"不支持的分流决定：{decision}")
    if decision == "wait" and not next_follow_up:
        raise ValueError("选择 wait 时必须提供 --follow-up，避免商机永久沉底")
    updated = update_opportunity(
        conn,
        opportunity_id,
        status=TRIAGE_DECISIONS[decision],
        stage=stage,
        priority=priority,
        next_action=next_action,
        next_follow_up=next_follow_up,
        note=note,
    )
    if decision in {"pursue", "wait", "won"}:
        add_feedback(
            conn,
            "opportunity",
            str(updated["opportunity_key"]),
            "confirmed",
            f"triage:{decision}",
        )
    elif decision == "ignore":
        add_feedback(
            conn,
            "opportunity",
            str(updated["opportunity_key"]),
            "ignore",
            "triage:ignore",
        )
    refreshed = conn.execute(
        "select * from opportunities where id = ?",
        (opportunity_id,),
    ).fetchone()
    return dict(refreshed) if refreshed else updated


def add_feedback(
    conn: sqlite3.Connection,
    target_type: str,
    target_key: str,
    verdict: str,
    note: str = "",
) -> int:
    if verdict not in FEEDBACK_VERDICTS:
        raise ValueError(f"不支持的反馈结论：{verdict}")
    if not target_type.strip() or not target_key.strip():
        raise ValueError("反馈对象类型和对象值不能为空")
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute(
        """
        insert into opportunity_feedback(target_type, target_key, verdict, note, created_at)
        values (?, ?, ?, ?, ?)
        """,
        (target_type.strip(), target_key.strip(), verdict, note.strip(), created_at),
    )
    target_type = target_type.strip()
    target_key = target_key.strip()
    if target_type == "chat":
        if verdict in {"false_positive", "ignore"}:
            conn.execute(
                """
                update opportunities
                set status = 'ignored', updated_at = ?
                where chat = ? and status in ('new', 'active', 'waiting', 'paused')
                """,
                (created_at, target_key.strip()),
            )
    elif target_type == "opportunity":
        key_clause = "opportunity_key = ?"
        lookup_key: Any = target_key
        if target_key.isdigit():
            key_clause = "id = ?"
            lookup_key = int(target_key)
        if verdict in {"false_positive", "ignore"}:
            conn.execute(
                f"""
                update opportunities
                set status = 'ignored', last_reviewed_at = ?, updated_at = ?
                where {key_clause} and status in ('new', 'active', 'waiting', 'paused', 'stale')
                """,
                (created_at, created_at, lookup_key),
            )
        elif verdict == "low_priority":
            conn.execute(
                f"""
                update opportunities
                set priority = min(priority, 1), priority_locked = 1,
                    last_reviewed_at = ?, updated_at = ?
                where {key_clause} and status in ('new', 'active', 'waiting', 'paused', 'stale')
                """,
                (created_at, created_at, lookup_key),
            )
        elif verdict == "confirmed":
            conn.execute(
                f"""
                update opportunities
                set record_type = 'opportunity', confidence = 'confirmed',
                    priority = max(priority, 5), expires_at = '',
                    last_reviewed_at = ?, updated_at = ?
                where {key_clause} and status in ('new', 'active', 'waiting', 'paused', 'stale')
                """,
                (created_at, created_at, lookup_key),
            )
        elif verdict == "low_priority":
            conn.execute(
                """
                update opportunities
                set priority = min(priority, 1), priority_locked = 1, updated_at = ?
                where chat = ? and status in ('new', 'active', 'waiting', 'paused')
                """,
                (created_at, target_key.strip()),
            )
        elif verdict == "confirmed":
            conn.execute(
                """
                update opportunities
                set priority = max(priority, 5), updated_at = ?
                where chat = ? and status in ('new', 'active', 'waiting', 'paused')
                """,
                (created_at, target_key.strip()),
            )
    return int(cursor.lastrowid)


def list_feedback(
    conn: sqlite3.Connection,
    *,
    target_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    sql = "select * from opportunity_feedback where 1 = 1"
    params: list[Any] = []
    if target_type:
        sql += " and target_type = ?"
        params.append(target_type)
    sql += " order by id desc limit ?"
    params.append(max(1, limit))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]

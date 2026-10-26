"""Regressions for defects reproduced in the 2026-09-28 audit. All data is synthetic."""

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import intelligence_views
import opportunity_store
import wechat_intelligence_hub as radar
from report_bundle_flagship import (
    build_report_pages,
    discover_report_sources,
    unmapped_report_sources,
)


def message(chat: str, sender: str, time: str, content: str) -> radar.Message:
    return radar.Message(chat, sender, time, content, "wechat-cli:timeline:private")


class AuditRegressionBase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        radar.init_radar_db(self.conn)
        cursor = self.conn.execute(
            """
            insert into runs(run_type, since_time, until_time, created_at, groups_count, messages_count, output_dir)
            values ('test', '', '', '2026-09-28 10:00:00', 1, 1, 'test')
            """
        )
        self.run_id = int(cursor.lastrowid)

    def tearDown(self) -> None:
        self.conn.close()

    def insert(self, rows: list[radar.Message]) -> None:
        radar.insert_messages_to_db(self.conn, self.run_id, rows, "2026-09-28 10:00:00")


class BriefCoverageTests(AuditRegressionBase):
    def rows(self, count: int) -> list[radar.Message]:
        return [
            message("联系人A", "联系人A", f"2026-09-28 08:{index:02d}:00", f"普通消息{index}")
            for index in range(count)
        ]

    def test_window_over_limit_keeps_newest_and_reports_gap(self) -> None:
        self.insert([*self.rows(6), message("品牌方B", "品牌方B", "2026-09-28 09:00:00", "方便确认报价吗？")])

        rows, coverage = intelligence_views._window_coverage(
            self.conn, "2026-09-28 00:00:00", "2026-09-29 00:00:00", limit=5
        )

        self.assertEqual(rows[-1]["content"], "方便确认报价吗？")
        self.assertEqual([row["time"] for row in rows], sorted(row["time"] for row in rows))
        self.assertEqual(coverage["total"], 7)
        self.assertEqual(coverage["read"], 5)
        self.assertEqual(coverage["omitted"], 2)
        self.assertTrue(coverage["truncated"])
        self.assertEqual(coverage["covered_since"], rows[0]["time"])

    def test_window_within_limit_is_complete_and_ordered(self) -> None:
        self.insert(self.rows(4))

        rows, coverage = intelligence_views._window_coverage(
            self.conn, "2026-09-28 00:00:00", "2026-09-29 00:00:00"
        )

        self.assertEqual([row["content"] for row in rows], [f"普通消息{index}" for index in range(4)])
        self.assertFalse(coverage["truncated"])
        self.assertEqual(coverage["omitted"], 0)

    def test_brief_states_truncation_instead_of_looking_complete(self) -> None:
        self.insert([*self.rows(6), message("品牌方B", "品牌方B", "2026-09-28 09:00:00", "方便确认报价吗？")])

        with mock.patch.object(intelligence_views, "WINDOW_MESSAGE_LIMIT", 3):
            text, metadata = intelligence_views.brief_report(
                self.conn, hours=24, now=datetime(2026, 9, 28, 12, 0, 0)
            )

        self.assertTrue(metadata["window_truncated"])
        self.assertEqual(metadata["window_total"], 7)
        self.assertEqual(metadata["window_omitted"], 4)
        self.assertIn("只纳入最新 3 条", text)
        self.assertIn("不能当作完整简报", text)
        self.assertIn("品牌方B", text)

    def test_brief_reports_full_coverage_when_nothing_is_omitted(self) -> None:
        self.insert(self.rows(3))

        text, metadata = intelligence_views.brief_report(
            self.conn, hours=24, now=datetime(2026, 9, 28, 12, 0, 0)
        )

        self.assertFalse(metadata["window_truncated"])
        self.assertIn("3 条全部纳入", text)


class SettlementStageTests(unittest.TestCase):
    def stages(self, content: str) -> list[str]:
        return [signal.stage for signal in radar.extract_signals([message("买方A", "买方A", "2026-09-28 08:00:00", content)])]

    def test_pre_deal_invoice_question_is_not_published_settlement(self) -> None:
        for content in (
            "合作之前先问下，可以开发票吗？",
            "你们能开专票吗",
            "请问怎么结算？",
            "付款方式是什么",
            "需要先付定金吗",
            "账期一般多久",
            "预付50%，发布后付尾款，可以吗",
            "发布满7天后按数据结算，这个规则你能接受吗",
            "如果合作，你这边是个人收款还是公司收款？",
            "个人能不能收款？我们公司只能对公",
            "开始之前确认下，你们是月结还是单结",
        ):
            with self.subTest(content=content):
                stages = self.stages(content)
                self.assertNotIn("已发布待结算", stages)
                self.assertIn("待确认报价/排期", stages)

    def test_actual_settlement_progress_stays_settlement(self) -> None:
        for content in (
            "发票已开，麻烦安排付款",
            "稿子已发布，可以开发票了吗",
            "尾款今天打过去了，查收一下",
            "invoice sent, payment pending",
            "尾款什么时候付",
            "这个月的推广费什么时候打款？",
            "数据已回传，麻烦确认下什么时候结算",
            "发票今天能开出来吗？错过今天要再等几天",
            "这边开票什么进度了呀",
        ):
            with self.subTest(content=content):
                self.assertIn("已发布待结算", self.stages(content))

    def test_account_wording_is_not_a_reconciliation_signal(self) -> None:
        self.assertEqual(self.stages("不建议四连太多，对账号不好"), [])

    def test_candidate_from_pre_deal_question_does_not_claim_settlement(self) -> None:
        candidates = radar.build_opportunity_candidates(
            [message("买方A", "买方A", "2026-09-28 08:00:00", "合作之前先问下，可以开发票吗？")]
        )

        self.assertEqual([candidate["stage"] for candidate in candidates], ["待确认报价/排期"])


class OpportunityStateTests(AuditRegressionBase):
    def candidate(self, stage: str = "已收款") -> int:
        opportunity_store.sync_opportunity_candidates(
            self.conn,
            self.run_id,
            [
                {
                    "chat": "联系人A",
                    "opportunity_key": "synthetic:one",
                    "stage": stage,
                    "record_type": "candidate",
                    "confidence": "medium",
                    "priority": 5,
                    "last_signal_time": "2026-09-28 09:00:00",
                }
            ],
            "2026-09-28 10:00:00",
        )
        return int(self.conn.execute("select id from opportunities").fetchone()[0])

    def record_type(self, opportunity_id: int) -> str:
        row = self.conn.execute("select record_type from opportunities where id = ?", (opportunity_id,)).fetchone()
        return str(row[0])

    def test_reopening_schema_does_not_promote_machine_inferred_stage(self) -> None:
        opportunity_id = self.candidate("已收款")

        opportunity_store.init_opportunity_schema(self.conn)

        self.assertEqual(self.record_type(opportunity_id), "candidate")
        self.assertEqual(opportunity_store.list_today_opportunities(self.conn, today="2026-09-28"), [])

    def test_human_stage_decision_still_promotes(self) -> None:
        opportunity_id = self.candidate("新线索")

        opportunity_store.update_opportunity(self.conn, opportunity_id, stage="待报价")
        opportunity_store.init_opportunity_schema(self.conn)

        self.assertEqual(self.record_type(opportunity_id), "opportunity")

    def test_legacy_database_is_migrated_once(self) -> None:
        legacy = sqlite3.connect(":memory:")
        legacy.row_factory = sqlite3.Row
        legacy.executescript(
            """
            create table runs(id integer primary key);
            create table messages(id integer primary key);
            create table opportunities (
                id integer primary key autoincrement,
                opportunity_key text not null default '',
                chat text not null, title text not null,
                opportunity_type text not null default '其他合作',
                stage text not null default '新线索', status text not null default 'new',
                priority integer not null default 0, amount text not null default '',
                last_signal_time text not null default '', next_action text not null default '',
                next_follow_up text not null default '', notes text not null default '',
                source_run_id integer, stage_locked integer not null default 0,
                priority_locked integer not null default 0, next_action_locked integer not null default 0,
                created_at text not null, updated_at text not null, closed_at text not null default ''
            );
            insert into opportunities(chat, title, stage, created_at, updated_at)
            values ('旧联系人', '旧记录', '待报价', '2026-07-01 09:00:00', '2026-07-01 09:00:00');
            """
        )

        opportunity_store.init_opportunity_schema(legacy)

        self.assertEqual(legacy.execute("select record_type from opportunities").fetchone()[0], "opportunity")
        legacy.close()

    def test_waiting_requires_a_follow_up_date(self) -> None:
        opportunity_id = self.candidate("待报价")

        with self.assertRaisesRegex(ValueError, "跟进日期"):
            opportunity_store.update_opportunity(self.conn, opportunity_id, status="waiting")
        updated = opportunity_store.update_opportunity(
            self.conn, opportunity_id, status="waiting", next_follow_up="2026-10-08"
        )

        self.assertEqual(updated["status"], "waiting")
        self.assertEqual(updated["next_follow_up"], "2026-10-08")
        with self.assertRaisesRegex(ValueError, "跟进日期"):
            opportunity_store.update_opportunity(self.conn, opportunity_id, clear_follow_up=True)

    def test_follow_up_date_must_be_a_real_date(self) -> None:
        opportunity_id = self.candidate("待报价")

        for value in ("下周", "2026-13-40", "10/08"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "格式无效"):
                opportunity_store.update_opportunity(self.conn, opportunity_id, next_follow_up=value)
        updated = opportunity_store.update_opportunity(
            self.conn, opportunity_id, next_follow_up="2026-10-08T09:30"
        )

        self.assertEqual(updated["next_follow_up"], "2026-10-08 09:30:00")

    def test_legacy_waiting_row_without_date_stays_visible(self) -> None:
        opportunity_id = self.candidate("待报价")
        self.conn.execute(
            "update opportunities set status = 'waiting', record_type = 'opportunity', next_follow_up = '' where id = ?",
            (opportunity_id,),
        )

        rows = opportunity_store.list_today_opportunities(self.conn, today="2026-09-28")

        self.assertEqual([row["id"] for row in rows], [opportunity_id])
        self.assertIn("没有跟进日期", radar.opportunity_rows_to_markdown(rows))


class ReplyDraftEvidenceTests(unittest.TestCase):
    def test_first_quote_request_does_not_invent_a_previous_price(self) -> None:
        draft = intelligence_views._reply_draft("quote", "第一次联系，请问一篇文章怎么报价？", "新联系人", {})

        self.assertNotIn("前面", draft)
        self.assertNotIn("单条", draft)
        self.assertIn("需求", draft)

    def test_per_item_claim_needs_the_owner_to_have_quoted_per_item(self) -> None:
        latest = "批量做的话价格怎么算"

        without_evidence = intelligence_views._reply_draft(
            "quote", latest, "商务联系人", {}, quote_context={"owner_quote": True}
        )
        with_evidence = intelligence_views._reply_draft(
            "quote", latest, "商务联系人", {}, quote_context={"owner_quote": True, "owner_per_item_quote": True}
        )

        self.assertNotIn("单条", without_evidence)
        self.assertIn("前面是单条定制价", with_evidence)

    def test_settlement_intent_outranks_advice_tone(self) -> None:
        draft = intelligence_views._reply_draft("settlement", "建议发票下周开", "新联系人", {})

        self.assertIn("结算", draft)
        self.assertNotIn("精简", draft)

    def test_generic_advice_does_not_promise_to_be_more_concise(self) -> None:
        draft = intelligence_views._reply_draft("general", "我觉得封面可以换一张", "新联系人", {})

        self.assertNotIn("精简", draft)


class ReportManifestTests(unittest.TestCase):
    def build(self, root: Path) -> None:
        (root / "group-daily").mkdir()
        (root / "final_report.md").write_text("# 总览\n\n- 行动一\n", encoding="utf-8")
        (root / "services_review.md").write_text("# 定制服务跟进\n\n- 虚构客户\n", encoding="utf-8")
        (root / "group-daily" / "cross_group_links.md").write_text("# 链接\n", encoding="utf-8")
        (root / "cross_group_links.md").write_text("# 链接\n", encoding="utf-8")

    def test_unmapped_topical_markdown_is_reported_not_silently_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            self.build(root)

            sources = discover_report_sources(root)
            shown = [source.relative_path for page in build_report_pages(sources) for source in page.sources]

            self.assertNotIn("services_review.md", shown)
            self.assertEqual([source.relative_path for source in unmapped_report_sources(sources)], ["services_review.md"])

    def test_manifest_routes_topical_markdown_into_an_existing_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            self.build(root)
            (root / "report_manifest.json").write_text(
                json.dumps({"sources": [{"path": "services_review.md", "page": "contacts"}]}),
                encoding="utf-8",
            )

            sources = discover_report_sources(root)
            pages = {page.route: [source.relative_path for source in page.sources] for page in build_report_pages(sources)}

            self.assertEqual(pages["contacts"], ["services_review.md"])
            self.assertEqual(unmapped_report_sources(sources), [])

    def test_manifest_rejects_unknown_page_and_paths_outside_the_report(self) -> None:
        for entry in (
            {"path": "services_review.md", "page": "appendix"},
            {"path": "../outside.md", "page": "overview"},
            {"path": "missing.md", "page": "overview"},
        ):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir).resolve() / "report"
                root.mkdir()
                self.build(root)
                (root.parent / "outside.md").write_text("# 外部\n", encoding="utf-8")
                (root / "report_manifest.json").write_text(json.dumps({"sources": [entry]}), encoding="utf-8")

                with self.assertRaises(ValueError):
                    discover_report_sources(root)


if __name__ == "__main__":
    unittest.main()

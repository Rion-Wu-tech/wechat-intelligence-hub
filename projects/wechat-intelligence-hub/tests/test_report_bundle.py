from pathlib import Path
import tempfile
import unittest

from report_bundle_html import build_combined_markdown, discover_report_sources
from report_bundle_flagship import (
    HTML_TEMPLATE,
    _normalize_details_markdown,
    _render_group_selector,
    _wrap_key_group_sections,
    build_report_pages,
    discover_report_sources as discover_flagship_sources,
    write_markdown_site,
)
from report_security import protect_document


class ReportBundleTests(unittest.TestCase):
    def test_flagship_appearance_preserves_single_script_security_boundary(self) -> None:
        document = protect_document(HTML_TEMPLATE)

        self.assertEqual(document.count("<script>"), 1)
        self.assertIn("Content-Security-Policy", document)
        for style in ("radar", "brief", "ledger"):
            self.assertIn(f'name="report-style" value="{style}"', document)
        for mode in ("system", "light", "dark"):
            self.assertIn(f'name="report-mode" value="{mode}"', document)
        self.assertIn("wechat-flagship-appearance", document)
        self.assertIn("document.documentElement.dataset.ready='true'", document)
        self.assertNotIn('id="theme-button"', document)

    def test_discovers_all_markdown_in_editorial_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "group-daily").mkdir()
            (root / "contact-daily").mkdir()
            (root / "final_report.md").write_text("# 总览\n", encoding="utf-8")
            (root / "group-daily" / "group_daily_brief.md").write_text("# 群聊\n", encoding="utf-8")
            (root / "contact-daily" / "contact_daily_brief.md").write_text("# 私信\n", encoding="utf-8")
            (root / "extra.md").write_text("# 额外信息\n", encoding="utf-8")
            (root / "wechat_daily_full.md").write_text("# 旧的整合文件\n", encoding="utf-8")

            sources = discover_report_sources(root)

            self.assertEqual(
                [source.relative_path for source in sources],
                [
                    "final_report.md",
                    "group-daily/group_daily_brief.md",
                    "contact-daily/contact_daily_brief.md",
                    "extra.md",
                ],
            )

    def test_combined_markdown_preserves_each_source_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "final_report.md").write_text("# 总览\n\n- 行动一\n", encoding="utf-8")
            (root / "links.md").write_text("# 链接\n\nhttps://example.com/source\n", encoding="utf-8")
            sources = discover_report_sources(root)

            combined = build_combined_markdown(
                sources,
                title="综合报告",
                generated_at="2026-08-28 16:00",
            )

            self.assertIn("# 综合报告", combined)
            self.assertIn("行动一", combined)
            self.assertIn("https://example.com/source", combined)
            self.assertNotIn("# 旧的整合文件", combined)

    def test_flagship_markdown_site_preserves_content_in_separate_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "group-daily").mkdir()
            (root / "contact-daily").mkdir()
            (root / "final_report.md").write_text("# 总览\n\nOVERVIEW_UNIQUE_TOKEN\n", encoding="utf-8")
            (root / "group-daily" / "group_daily_brief.md").write_text(
                "# 群聊\n\nGROUP_UNIQUE_TOKEN\n", encoding="utf-8"
            )
            (root / "contact-daily" / "contact_daily_brief.md").write_text(
                "# 私信\n\nCONTACT_UNIQUE_TOKEN\n", encoding="utf-8"
            )
            sources = discover_flagship_sources(root)
            pages = build_report_pages(sources)
            portal = root / "wechat_daily_full.md"

            write_markdown_site(
                root,
                pages,
                portal_path=portal,
                title="综合报告",
                generated_at="2026-08-29 10:00",
            )

            self.assertEqual([page.route for page in pages], ["overview", "groups", "contacts"])
            self.assertIn("OVERVIEW_UNIQUE_TOKEN", (root / "wechat-report" / "index.md").read_text(encoding="utf-8"))
            self.assertIn("GROUP_UNIQUE_TOKEN", (root / "wechat-report" / "groups.md").read_text(encoding="utf-8"))
            self.assertIn("CONTACT_UNIQUE_TOKEN", (root / "wechat-report" / "contacts.md").read_text(encoding="utf-8"))
            portal_text = portal.read_text(encoding="utf-8")
            self.assertIn("打开旗舰版交互日报", portal_text)
            self.assertNotIn("OVERVIEW_UNIQUE_TOKEN", portal_text)

    def test_generated_markdown_site_is_not_rediscovered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "final_report.md").write_text("# 总览\n", encoding="utf-8")
            (root / "wechat-report").mkdir()
            (root / "wechat-report" / "index.md").write_text("# 生成页\n", encoding="utf-8")

            sources = discover_flagship_sources(root)

            self.assertEqual([source.relative_path for source in sources], ["final_report.md"])

    def test_dual_group_reports_replace_legacy_brief_and_internal_files_are_not_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "group-daily").mkdir()
            (root / "contact-daily").mkdir()
            (root / "final_report.md").write_text("# 总览\n", encoding="utf-8")
            (root / "action_overview.md").write_text("# 行动\n", encoding="utf-8")
            (root / "group-daily" / "group_daily_topics.md").write_text("# 话题\n", encoding="utf-8")
            (root / "group-daily" / "group_daily_groups.md").write_text("# 重点群\n", encoding="utf-8")
            (root / "group-daily" / "group_daily_brief.md").write_text("# 旧版\n", encoding="utf-8")
            (root / "group-daily" / "group_daily_appendix.md").write_text("# 附录\n", encoding="utf-8")
            (root / "group-daily" / "cross_group_links.md").write_text("# 链接\n", encoding="utf-8")
            (root / "contact-daily" / "contact_daily_brief.md").write_text("# 联系人\n", encoding="utf-8")

            sources = discover_flagship_sources(root)
            pages = build_report_pages(sources)

            self.assertNotIn("group-daily/group_daily_brief.md", [source.relative_path for source in sources])
            self.assertEqual([page.route for page in pages], ["overview", "groups", "contacts", "radar"])
            self.assertEqual(
                [source.title for source in next(page for page in pages if page.route == "groups").sources],
                ["话题日报", "重点群聊"],
            )
            self.assertNotIn("internal", [page.route for page in pages])

    def test_leaked_details_markup_becomes_plain_markdown(self) -> None:
        text = "<details open>\n<summary><strong>Tencent Cloud Buddy</strong>｜Hy4 发布</summary>\n\n- 有效信息\n</details>"

        normalized = _normalize_details_markdown(text)

        self.assertIn("### Tencent Cloud Buddy", normalized)
        self.assertIn("> Hy4 发布", normalized)
        self.assertNotIn("<details", normalized)
        self.assertNotIn("<summary", normalized)

    def test_group_selector_exposes_all_personal_business_dimensions(self) -> None:
        payload = {
            "basis": "按最新计划筛选",
            "groups": [{
                "群聊": "AI 商业增长群",
                "消息数": 88,
                "有效讨论数": 30,
                "活跃度": "中",
                "AI": True,
                "自媒体运营与增长": True,
                "合作": True,
                "B端AI赋能": True,
                "AI消息数": 12,
                "自媒体运营与增长消息数": 5,
                "合作消息数": 3,
                "B端AI赋能消息数": 2,
                "建议关注级别": "重点",
                "群聊类型": "商业与合作",
                "判断依据": "出现企业培训需求",
                "主要主题": "企业 AI 与内容增长",
            }],
        }

        html = _render_group_selector(payload)

        self.assertIn("全部群聊筛选", html)
        self.assertIn("自媒体运营与增长", html)
        self.assertIn("B端AI赋能", html)
        self.assertIn("排除候选", html)
        self.assertIn("matrix-detail", html)

    def test_key_groups_become_clickable_disclosures(self) -> None:
        html = '<h2 id="group-a">群 A</h2><p>细节 A</p><h2 id="group-b">群 B</h2><p>细节 B</p>'

        wrapped = _wrap_key_group_sections(html)

        self.assertEqual(wrapped.count('<details class="key-group-card"'), 2)
        self.assertIn("点击展开", wrapped)
        self.assertIn("细节 B", wrapped)


if __name__ == "__main__":
    unittest.main()

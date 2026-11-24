import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import wechat_intelligence_hub as radar


def message(chat: str, sender: str, content: str) -> radar.Message:
    return radar.Message(
        chat=chat,
        sender=sender,
        time="2026-07-11 12:00:00",
        content=content,
        source_file="test",
    )


def timed_message(chat: str, sender: str, time: str, content: str) -> radar.Message:
    return radar.Message(
        chat=chat,
        sender=sender,
        time=time,
        content=content,
        source_file="test",
    )


class CrossGroupDealProbabilityTests(unittest.TestCase):
    def test_group_daily_machine_html_is_opt_in(self) -> None:
        parser = radar.build_parser()

        default_args = parser.parse_args(["group-daily"])
        html_args = parser.parse_args(["group-daily", "--html", "--hours", "48"])

        self.assertFalse(default_args.html)
        self.assertTrue(html_args.html)
        self.assertEqual(html_args.hours, 48)

    def test_text_group_recap_is_not_reused_as_topic_or_opportunity_evidence(self) -> None:
        summary = radar.summarize_group(
            [
                timed_message(
                    "AI自媒体交流群",
                    "Rachel",
                    "2026-08-24 23:10:00",
                    "## 0824 群日报\n今日品牌方招募 AI 博主，预算 3000 元，还有 5 个名额。",
                ),
                timed_message(
                    "AI自媒体交流群",
                    "群友",
                    "2026-08-24 23:20:00",
                    "今天试了一下 Codex 的新工作流，整体还不错。",
                ),
            ]
        )

        self.assertEqual(summary["已有日报数"], "1")
        self.assertEqual(summary["有效消息数"], "1")
        self.assertEqual(summary["商单机会数"], "0")
        self.assertNotIn("品牌方招募", summary["关键发言"])
        self.assertNotIn("品牌方招募", summary["主要主题"])

    def test_configured_image_recap_sender_respects_time_window(self) -> None:
        profile = radar.merge_profile(
            radar.DEFAULT_PROFILE,
            {
                "group_recap_senders": {"TATALAB": ["那时年少"]},
                "group_recap_time_windows": {"TATALAB": ["06:50-07:20"]},
            },
        )
        messages = [
            timed_message("TATALAB👾一起早起", "那时年少", "2026-08-25 07:00:00", "[图片]"),
            timed_message("TATALAB👾一起早起", "那时年少", "2026-08-25 07:42:00", "[图片]"),
            timed_message("TATALAB👾一起早起", "群友", "2026-08-25 08:00:00", "今天聊了 Agent 工作流。"),
        ]

        with patch.object(radar, "ACTIVE_PROFILE", profile):
            summary = radar.summarize_group(messages)

        self.assertEqual(summary["已有日报数"], "1")
        self.assertEqual(summary["有效消息数"], "2")
        recaps = radar.load_group_recaps(summary)
        self.assertEqual(recaps[0]["时间"], "2026-08-25 07:00:00")

    def test_discussing_a_group_recap_feature_is_not_itself_a_recap(self) -> None:
        row = timed_message(
            "TATALAB👾一起早起",
            "Kin",
            "2026-08-23 17:20:55",
            "我们群里是不是有个群聊总结功能？是哪个机器人在发？",
        )

        self.assertFalse(radar.is_group_recap_message(row))

    def test_group_recap_is_counted_without_forcing_irrelevant_group_into_html(self) -> None:
        summary = radar.summarize_group(
            [
                timed_message("AI群", "Rachel", "2026-08-24 23:10:00", "## 0824 群日报\n今日讨论总结"),
                timed_message("AI群", "群友", "2026-08-24 23:20:00", "Agent 工作流有了新进展"),
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            md_path = Path(temp_dir) / "group_daily_digest.md"
            html_path = Path(temp_dir) / "group_daily_digest.html"
            radar.write_group_daily_digest(
                md_path,
                [summary],
                "2026-08-24 00:00:00",
                "2026-08-25 00:00:00",
                [],
            )
            radar.write_group_daily_html(
                html_path,
                [summary],
                "2026-08-24 00:00:00",
                "2026-08-25 00:00:00",
                [],
            )
            markdown = md_path.read_text(encoding="utf-8")
            html = html_path.read_text(encoding="utf-8")

        self.assertIn("群内已有日报", markdown)
        self.assertIn("已从本报告主题与证据中排除", markdown)
        self.assertIn("群内已有日报", html)
        self.assertNotIn("今日讨论总结", html)

    def test_cross_group_paid_boost_is_high_probability_deal(self) -> None:
        messages = [
            message("加热群1", "博主A 19k", "https://x.com/a/status/1 三连3元，四连5元，截图结算"),
            message("加热群2", "中间人", "https://x.com/a/status/1 #接龙 限10个名额"),
        ]

        assessment, reason, rank = radar.assess_link_deal_probability(messages, chat_count=2)

        self.assertEqual(assessment, "高概率商单")
        self.assertIn("万粉", reason)
        self.assertEqual(rank, 3)

    def test_explicit_non_deal_share_is_not_promoted(self) -> None:
        messages = [
            message("交流群", "Allen", "https://x.com/a/status/2 不是商单，纯分享"),
        ]

        assessment, _, rank = radar.assess_link_deal_probability(messages, chat_count=1)

        self.assertEqual(assessment, "明确非商单")
        self.assertEqual(rank, 0)

    def test_explicit_non_deal_wins_over_paid_boost_signals(self) -> None:
        messages = [
            message("加热群1", "发布者", "https://x.com/a/status/22 不是商单，三连1元，限15人"),
            message("加热群2", "好友", "https://x.com/a/status/22 #接龙 四连2元"),
        ]

        assessment, reason, rank = radar.assess_link_deal_probability(messages, chat_count=2)

        self.assertEqual(assessment, "明确非商单")
        self.assertIn("原文明确", reason)
        self.assertEqual(rank, 0)

    def test_short_term_project_with_concrete_revenue_is_actionable_money(self) -> None:
        summary = radar.summarize_group(
            [
                message("项目群", "发起人", "这是豆包拉新的短平快项目，时间窗口比较短"),
                message("项目群", "发起人", "做一个视频几分钟，收益破千"),
            ]
        )

        self.assertEqual(summary["赚钱/奖励信号数"], "1")
        self.assertNotEqual(summary["建议动作"], "低优先级，不用看")

    def test_cross_group_organic_distribution_is_only_suspected(self) -> None:
        messages = [
            message("群1", "博主", "https://x.com/a/status/3 欢迎看看"),
            message("群2", "好友", "https://x.com/a/status/3 这篇写得不错"),
        ]

        assessment, _, rank = radar.assess_link_deal_probability(messages, chat_count=2)

        self.assertEqual(assessment, "疑似商单")
        self.assertEqual(rank, 1)

    def test_viral_view_count_is_not_mistaken_for_follower_count(self) -> None:
        messages = [
            message("群1", "博主A", "最近连续出了几个10万+爆款，欢迎看看 https://x.com/a/status/4"),
            message("群2", "博主A", "这篇复盘也同步一下 https://x.com/a/status/4"),
            message("群3", "博主A", "欢迎交流 https://x.com/a/status/4"),
        ]

        assessment, reason, rank = radar.assess_link_deal_probability(messages, chat_count=3)

        self.assertEqual(assessment, "疑似商单")
        self.assertNotIn("万粉", reason)
        self.assertEqual(rank, 1)

    def test_social_profile_url_is_not_promoted_as_campaign(self) -> None:
        rows = radar.build_link_rows(
            {
                "https://x.com/example_creator": [
                    message("项目群1", "中间人", "付费合作博主主页 https://x.com/example_creator"),
                    message("项目群2", "品牌方", "红包加热 https://x.com/example_creator"),
                ]
            }
        )

        self.assertEqual(rows[0]["商单判断"], "普通内容")
        self.assertEqual(rows[0]["商单概率等级"], "0")
        self.assertIn("账号主页", rows[0]["判断依据"])

    def test_joined_channel_invite_is_not_counted_as_new_opportunity(self) -> None:
        messages = [
            message("示例博主·示例共创群", "示例博主A", "想进飞书群接商单的朋友扫码，通过审核就可以进群报名接单"),
        ]

        summary = radar.summarize_group(messages, ["示例博主A"])

        self.assertEqual(summary["变现机会数"], "0")
        self.assertEqual(summary["建议动作"], "低优先级，不用看")

    def test_joined_channel_new_brief_is_still_counted(self) -> None:
        messages = [
            message("示例博主·示例共创群", "示例博主A", "新一轮 AI 工具商单招募博主，预算 1500，今晚截止"),
        ]

        summary = radar.summarize_group(messages, ["示例博主A"])

        self.assertEqual(summary["变现机会数"], "1")
        self.assertIn("优先点开原群", summary["建议动作"])

    def test_natural_conversation_deal_without_link_is_detected(self) -> None:
        messages = [
            message("资源群", "中间人", "品牌方在找AI博主，这一批还有5个名额，想接的私聊我"),
        ]

        summary = radar.summarize_group(messages)

        self.assertEqual(summary["商单机会数"], "1")
        self.assertEqual(summary["培训/项目合作机会数"], "0")
        self.assertIn("品牌方在找AI博主", summary["商单机会摘要"])

    def test_training_project_uses_conversation_and_flags_adjacent_image(self) -> None:
        messages = [
            message("X商单共享联盟", "资源方A", "[图片]"),
            message("X商单共享联盟", "资源方A", "你们有想搞企业培训的吗"),
            message("X商单共享联盟", "创作者A", "报名"),
        ]

        summary = radar.summarize_group(messages)

        self.assertEqual(summary["培训/项目合作机会数"], "1")
        self.assertIn("企业培训", summary["培训/项目合作摘要"])
        self.assertIn("图片", summary["附件核验提示"])
        self.assertIn("培训/咨询/项目合作线索", summary["建议动作"])

    def test_group_market_discussion_stays_out_of_actionable_digest(self) -> None:
        messages = [
            message("创作者交流群", "黄白", "有人知道 X 怎么报价合适吗"),
            message("创作者交流群", "Allen", "国内 AI 品牌的商单通常会看粉丝量和数据"),
        ]

        summary = radar.summarize_group(messages)

        self.assertEqual(summary["商单机会数"], "0")
        self.assertEqual(summary["变现机会数"], "0")
        self.assertEqual(summary["建议动作"], "低优先级，不用看")

    def test_group_training_experience_is_not_a_training_opportunity(self) -> None:
        messages = [
            message("AI 交流群", "Jacky", "企业内 FDE 和培训转型都可以聊聊"),
            message("AI 交流群", "小王", "我以前考过电商讲师资格证"),
        ]

        summary = radar.summarize_group(messages)

        self.assertEqual(summary["培训/项目合作机会数"], "0")

    def test_course_content_discussion_is_not_a_training_opportunity(self) -> None:
        summary = radar.summarize_group(
            [message("内容交流群", "群友", "按照拆选题、找对标的方法，把这个课程内容转化成公众号文章和视频")]
        )

        self.assertEqual(summary["培训/项目合作机会数"], "0")
        self.assertEqual(summary["重要信号数"], "0")
        self.assertEqual(summary["建议动作"], "低优先级，不用看")

    def test_discussion_threads_split_on_time_gap_and_keep_topics(self) -> None:
        messages = [
            timed_message("AI 群", "A", "2026-07-11 09:00:00", "试了新的 Agent 工作流"),
            timed_message("AI 群", "B", "2026-07-11 09:08:00", "Codex 做自动化的效果还不错"),
            timed_message("AI 群", "C", "2026-07-11 11:10:00", "明天举办一场 AI workshop，还有 5 个报名名额"),
        ]

        threads = radar.build_group_discussion_threads(messages)

        self.assertEqual(len(threads), 2)
        self.assertIn("AI 产品/模型", threads[0]["主题"])
        self.assertIn("活动", threads[1]["信号类型"])

    def test_actionable_event_has_separate_signal_and_next_step(self) -> None:
        summary = radar.summarize_group(
            [message("澳洲 AI 群", "主办方", "明天举办 10 人 AI workshop，现在可以报名，也在找分享嘉宾")]
        )

        self.assertEqual(summary["活动信号数"], "1")
        self.assertIn("主办方", summary["建议动作"])

    def test_plain_today_reference_is_not_a_deadline(self) -> None:
        threads = radar.build_group_discussion_threads(
            [message("普通群", "群友", "今天试了一下新模型，效果不错")]
        )

        self.assertEqual(threads[0]["关注等级"], "背景动态")

    def test_short_boost_reactions_do_not_pollute_group_threads(self) -> None:
        threads = radar.build_group_discussion_threads(
            [
                timed_message("商单群", "A", "2026-07-11 12:00:00", "三连"),
                timed_message("商单群", "B", "2026-07-11 12:01:00", "已三连"),
                timed_message(
                    "商单群",
                    "中间人",
                    "2026-07-11 12:02:00",
                    "明天截止，品牌还需要3位AI博主",
                ),
            ]
        )

        self.assertEqual(len(threads), 1)
        evidence = " ".join(
            item["内容"] for item in threads[0]["证据消息"]
        )
        self.assertNotIn("三连", evidence)

    def test_boost_only_message_stays_out_of_group_digest_but_real_deal_survives(self) -> None:
        boost_only = radar.summarize_group(
            [message("娱乐群", "群友", "红包加热，三连 2 元，截图结算 https://x.com/example/status/1")]
        )
        actual_deal = radar.summarize_group(
            [message("商单群", "中间人", "Topview 商单招募 AI 博主，预算 1500 元，今晚截止，三连 2 元")]
        )

        self.assertEqual(boost_only["商单机会数"], "0")
        self.assertEqual(boost_only["重要讨论数"], "0")
        self.assertEqual(actual_deal["商单机会数"], "1")
        self.assertGreater(int(actual_deal["重要讨论数"]), 0)

    def test_group_selection_matrix_matches_current_personal_priorities(self) -> None:
        messages = [
            message("企业增长群", "项目方", "找企业 AI 赋能和工作流培训合作，也想做视频号内容增长"),
            message("周末球友闲聊群", "群友", "今晚足球比分和聚餐聊得太热闹了"),
        ]
        summaries = [
            radar.summarize_group([messages[0]]),
            radar.summarize_group([messages[1]]),
        ]

        rows = radar.build_group_selection_rows(summaries, messages)
        by_group = {row["群聊"]: row for row in rows}

        business = by_group["企业增长群"]
        self.assertTrue(business["B端AI赋能"])
        self.assertTrue(business["自媒体运营与增长"])
        self.assertTrue(business["合作"])
        self.assertEqual(business["建议关注级别"], "重点")

        leisure = by_group["周末球友闲聊群"]
        self.assertEqual(leisure["群聊类型"], "低价值娱乐/闲聊")
        self.assertEqual(leisure["建议关注级别"], "低优先级")

    def test_talking_about_a_deal_search_system_is_not_a_new_deal(self) -> None:
        row = message(
            "产品讨论群",
            "群友",
            "如果允许读取之前的所有商单，并提供几个相关性高的备选，会更清晰",
        )

        self.assertNotIn("商单", radar.discussion_signal_categories(row))

    def test_software_install_story_is_not_a_job_opening(self) -> None:
        row = message(
            "技术群",
            "群友",
            "找 IT 开发安装一个东西，他们只管安装指定版本",
        )

        self.assertNotIn("招聘/外包", radar.discussion_signal_categories(row))

    def test_event_headcount_guarantee_is_not_money_opportunity(self) -> None:
        row = message("活动群", "群友", "场地大，保底就 108 个人参与了")

        self.assertNotIn("赚钱/奖励", radar.discussion_signal_categories(row))

    def test_campaign_open_application_is_a_deal_opportunity(self) -> None:
        row = message(
            "商单群",
            "中间人",
            "大单来了！LLM Campaign 现已开放申请，品牌方会筛选参与原创的达人",
        )

        self.assertIn("商单", radar.discussion_signal_categories(row))

    def test_ad_budget_invitation_is_a_deal_opportunity(self) -> None:
        row = message(
            "运营群",
            "渠道方",
            "最近还有投放广告，有一大笔预算，公众号和朋友圈好的可以私信我",
        )

        self.assertIn("商单", radar.discussion_signal_categories(row))

    def test_hackathon_recruitment_is_an_event_not_a_job(self) -> None:
        row = message(
            "创作群",
            "主办方",
            "[招募] VibeHacks #05 报名开启，24 小时做出一个作品",
        )

        categories = radar.discussion_signal_categories(row)
        self.assertIn("活动", categories)
        self.assertNotIn("招聘/外包", categories)

    def test_high_volume_chat_is_editorial_input_but_not_automatically_important(self) -> None:
        messages = [
            timed_message(
                "普通AI群",
                f"群友{index}",
                f"2026-07-11 12:{index:02d}:00",
                "大家觉得 Codex 和其他工具有什么区别，实际使用方法是什么？",
            )
            for index in range(8)
        ]

        summary = radar.summarize_group(messages)
        threads = radar.load_discussion_threads(summary)
        packet = radar.build_group_editorial_packet(
            [summary],
            "2026-07-11 00:00:00",
            "2026-07-12 00:00:00",
            [],
        )

        self.assertEqual(summary["重要讨论数"], "0")
        self.assertEqual(threads[0]["关注等级"], "背景动态")
        self.assertTrue(threads[0]["证据消息"])
        self.assertEqual(packet["覆盖"]["讨论候选"], 1)
        self.assertEqual(packet["讨论候选"][0]["群聊"], "普通AI群")

    def test_digest_keeps_main_report_short_and_moves_timelines_to_appendix(self) -> None:
        actionable = radar.summarize_group(
            [message("商单资源群", "中间人", "品牌方招募 AI 博主，预算 3000 元，还有 3 个名额")]
        )
        quiet = radar.summarize_group(
            [message("普通交流群", "群友", "今天试了一下新模型，效果还不错")]
        )
        link_rows = [
            {
                "链接": "https://x.com/example/status/123",
                "类型": "高概率商单/加热链接",
                "商单判断": "高概率商单",
                "判断依据": "同链接跨群付费加热",
                "商单概率等级": "2",
                "出现次数": "4",
                "覆盖群数": "2",
                "相关群聊": "商单资源群、另一个群",
                "机会信号数": "4",
                "首次出现": "2026-07-11 10:00:00",
                "最后出现": "2026-07-11 12:00:00",
                "证据摘要": "",
            }
        ]
        actionable["跨群高概率商单"] = "1"

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "group_daily_digest.md"
            html_path = Path(temp_dir) / "group_daily_digest.html"
            radar.write_group_daily_digest(
                path,
                [actionable, quiet],
                "2026-07-11 00:00:00",
                "2026-07-12 00:00:00",
                link_rows,
            )
            radar.write_group_daily_html(
                html_path,
                [actionable, quiet],
                "2026-07-11 00:00:00",
                "2026-07-12 00:00:00",
                link_rows,
            )
            digest = path.read_text(encoding="utf-8")
            html = html_path.read_text(encoding="utf-8")
            appendix = path.with_name("group_daily_appendix.md").read_text(encoding="utf-8")

        self.assertIn("## 先处理这些", digest)
        self.assertIn("## 按群查看", digest)
        self.assertNotIn("<details>", digest)
        self.assertNotIn("普通交流群", digest)
        self.assertIn("讨论段落", appendix)
        self.assertIn("今天试了一下新模型", appendix)
        self.assertEqual(digest.count("https://x.com/example/status/123"), 1)
        self.assertIn("data-kind=\"action\"", html)
        self.assertIn("搜索群名、主题或信号", html)
        self.assertEqual(html.count("https://x.com/example/status/123"), 2)
        self.assertNotIn("普通交流群", html)

    def test_group_daily_continues_when_one_group_timeline_fails(self) -> None:
        sessions = [
            {"display_name": "正常群", "username": "ok@chatroom"},
            {"display_name": "异常群", "username": "bad@chatroom"},
        ]

        def timeline(_cli, session, _since, _until, _limit):
            if session["display_name"] == "异常群":
                raise SystemExit("temporary timeline failure")
            return [message("正常群", "群友", "今天讨论了 Agent 工作流")]

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            radar, "require_compatible_wechat_cli", return_value=Path("/tmp/fake-cli")
        ), patch.object(radar, "fetch_group_sessions", return_value=sessions), patch.object(
            radar, "timeline_messages_for_group", side_effect=timeline
        ), patch.object(radar.time, "sleep", return_value=None):
            radar.group_daily(
                argparse.Namespace(
                    wechat_cli="fake",
                    out=temp_dir,
                    since="2026-07-11 00:00:00",
                    until="2026-07-12 00:00:00",
                    exclude_list="",
                    joined_channel_list="",
                    group_limit=60,
                    per_group_limit=500,
                    min_link_chats=2,
                    no_db=True,
                    db=":memory:",
                )
            )
            digest = (Path(temp_dir) / "group_daily_digest.md").read_text(encoding="utf-8")
            appendix = (Path(temp_dir) / "group_daily_appendix.md").read_text(encoding="utf-8")
            coverage = (Path(temp_dir) / "group_daily_coverage.json").read_text(encoding="utf-8")

        self.assertIn("读取成功 1/2", digest)
        self.assertIn('"failed": 1', coverage)
        self.assertNotIn("正常群", digest)
        self.assertIn("正常群", appendix)


if __name__ == "__main__":
    unittest.main()

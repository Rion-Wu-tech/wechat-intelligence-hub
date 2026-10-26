from datetime import datetime, timezone
import sqlite3
import unittest

import intelligence_views
import wechat_intelligence_hub as radar


def message(
    chat: str,
    sender: str,
    time: str,
    content: str,
    source_file: str = "wechat-cli:timeline:private",
) -> radar.Message:
    return radar.Message(chat, sender, time, content, source_file)


class IntelligenceViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        radar.init_radar_db(self.conn)
        cursor = self.conn.execute(
            """
            insert into runs(run_type, since_time, until_time, created_at, groups_count, messages_count, output_dir)
            values ('test', '', '', '2026-08-02 12:00:00', 1, 1, 'test')
            """
        )
        self.run_id = int(cursor.lastrowid)

    def tearDown(self) -> None:
        self.conn.close()

    def insert(self, rows: list[radar.Message]) -> None:
        radar.insert_messages_to_db(self.conn, self.run_id, rows, "2026-08-02 12:00:00")

    def test_person_report_surfaces_promises_requests_and_open_opportunity(self) -> None:
        self.insert(
            [
                message("联系人A", "联系人A", "2026-08-01 09:00:00", "方便把课程brief发我吗？"),
                message("联系人A", "创作者A", "2026-08-01 09:05:00", "我明天整理课程框架发你"),
                message("联系人A", "联系人A", "2026-08-01 09:10:00", "好的，麻烦确认一下课程受众"),
            ]
        )
        self.conn.execute(
            """
            insert into opportunities(
                opportunity_key, chat, title, opportunity_type, stage, status, priority,
                last_signal_time, next_action, created_at, updated_at
            ) values ('private:test', '联系人A', '企业培训A', '培训/咨询/项目合作',
                      '待 brief', 'active', 5, '2026-08-01 09:05:00',
                      '发送课程框架', '2026-08-01 09:05:00', '2026-08-01 09:05:00')
            """
        )

        text, metadata = intelligence_views.person_report(self.conn, "联系人A", self_names=["创作者A"])

        self.assertEqual(metadata["chat"], "联系人A")
        self.assertIn("我答应过的事项", text)
        self.assertIn("整理课程框架发你", text)
        self.assertIn("课程brief", text)
        self.assertIn("发送课程框架", text)
        self.assertIn("先回复对方最后一条消息", text)

    def test_person_resolution_rejects_ambiguous_partial_name(self) -> None:
        self.insert(
            [
                message("联系人B培训", "联系人B", "2026-08-01 09:00:00", "培训项目"),
                message("联系人B品牌", "联系人B", "2026-08-01 10:00:00", "品牌合作"),
            ]
        )

        with self.assertRaisesRegex(ValueError, "匹配多个会话"):
            intelligence_views.person_report(self.conn, "联系人B")

    def test_topic_report_uses_known_expansions_across_private_and_group_chats(self) -> None:
        self.insert(
            [
                message("联系人A", "联系人A", "2026-08-01 09:00:00", "企业课程找讲师"),
                message(
                    "AI资源群",
                    "小王",
                    "2026-08-01 10:00:00",
                    "企业工作坊需要授课老师",
                    "wechat-cli:timeline:123@chatroom",
                ),
            ]
        )

        text, metadata = intelligence_views.topic_report(
            self.conn,
            "培训",
            since="2026-08-01",
        )

        self.assertEqual(metadata["chats"], 2)
        self.assertIn("讲师", metadata["terms"])
        self.assertIn("AI资源群｜群聊", text)
        self.assertIn("联系人A｜私聊", text)

    def test_brief_compares_windows_and_flags_pending_reply(self) -> None:
        self.insert(
            [
                message("旧联系人", "旧联系人", "2026-07-31 18:00:00", "之前的合作"),
                message("品牌方A", "品牌方A", "2026-08-02 10:00:00", "方便确认一下报价吗？"),
                message(
                    "AI资源群",
                    "小王",
                    "2026-08-02 11:00:00",
                    "企业培训项目找讲师",
                    "wechat-cli:timeline:123@chatroom",
                ),
            ]
        )

        text, metadata = intelligence_views.brief_report(
            self.conn,
            hours=24,
            now=datetime(2026, 8, 2, 12, 0, 0),
        )

        self.assertEqual(metadata["messages"], 2)
        self.assertEqual(metadata["previous_messages"], 1)
        self.assertEqual(metadata["pending_replies"], 1)
        self.assertIn("品牌方A", text)
        self.assertIn("消息 +1", text)
        self.assertIn("培训", text)

    def test_brief_detects_natural_price_question(self) -> None:
        self.insert(
            [
                message(
                    "Ａ－小杨",
                    "Ａ－小杨",
                    "2026-08-17 15:03:52",
                    "你这边根据体验做个测评创意视频，大致价位是多少呢",
                )
            ]
        )

        text, metadata = intelligence_views.brief_report(
            self.conn,
            hours=24,
            now=datetime(2026, 8, 18, 2, 0, 0),
        )

        self.assertEqual(metadata["pending_replies"], 1)
        self.assertIn("Ａ－小杨", text)
        self.assertIn("大致价位是多少呢", text)

    def test_brief_keeps_project_group_promise_open_after_ack(self) -> None:
        source = "wechat-cli:timeline:proj-a@chatroom"
        self.insert(
            [
                message(
                    "项目A 8月合作-终稿",
                    "玖",
                    "2026-08-17 09:44:44",
                    "创作者老师，帖子浏览量只有6k多，麻烦增加曝光并找KOL朋友转发",
                    source,
                ),
                message(
                    "项目A 8月合作-终稿",
                    "创作者A",
                    "2026-08-17 09:50:00",
                    "好的老师，我会多quote几次，同时让其他博主帮忙加热",
                    source,
                ),
                message(
                    "项目A 8月合作-终稿",
                    "玖",
                    "2026-08-17 10:07:00",
                    "嗯嗯对的",
                    source,
                ),
            ]
        )

        text, metadata = intelligence_views.brief_report(
            self.conn,
            hours=24,
            now=datetime(2026, 8, 18, 2, 0, 0),
            self_names=["创作者A"],
        )

        self.assertEqual(metadata["pending_replies"], 0)
        self.assertEqual(metadata["open_promises"], 1)
        self.assertIn("待兑现承诺", text)
        self.assertIn("多quote几次", text)
        self.assertIn("Quote/KOL 转发", text)

    def test_stale_brief_does_not_present_raw_delta_as_reliable_trend(self) -> None:
        self.insert(
            [message("品牌方A", "品牌方A", "2026-08-02 06:00:00", "方便确认报价吗？")]
        )

        text, metadata = intelligence_views.brief_report(
            self.conn,
            hours=24,
            now=datetime(2026, 8, 2, 12, 0, 0),
        )

        self.assertGreater(metadata["freshness_hours"], 2)
        self.assertIn("暂不解读环比", text)

    def test_brief_does_not_compare_windows_with_uneven_coverage(self) -> None:
        rows = [
            message("旧群", "A", "2026-08-01 10:00:00", "旧窗口消息"),
            message("旧群", "B", "2026-08-01 10:01:00", "旧窗口补充"),
        ]
        rows.extend(
            message(
                f"新群{index}",
                f"群友{index}",
                f"2026-08-02 11:{index:02d}:00",
                "新窗口消息",
            )
            for index in range(20)
        )
        self.insert(rows)

        text, metadata = intelligence_views.brief_report(
            self.conn,
            hours=24,
            now=datetime(2026, 8, 2, 12, 0, 0),
        )

        self.assertFalse(metadata["coverage_comparable"])
        self.assertIn("覆盖差异过大", text)
        self.assertIn("暂不解读环比", text)

    def test_brief_separates_unreviewed_candidates_from_tracked_opportunities(self) -> None:
        self.insert(
            [message("资源群", "中间人", "2026-08-02 10:00:00", "品牌方招募 AI 博主")]
        )
        self.conn.executemany(
            """
            insert into opportunities(
                opportunity_key, chat, title, opportunity_type, stage, status, priority,
                last_signal_time, next_action, created_at, updated_at
            ) values (?, ?, ?, '商单/推广', '新线索', ?, 5, ?, ?, ?, ?)
            """,
            [
                (
                    "candidate:test",
                    "资源群",
                    "待审核推广",
                    "new",
                    "2026-08-02 10:00:00",
                    "人工确认",
                    "2026-08-02 10:00:00",
                    "2026-08-02 10:00:00",
                ),
                (
                    "active:test",
                    "品牌方A",
                    "已确认合作",
                    "active",
                    "2026-08-02 10:30:00",
                    "回复 brief",
                    "2026-08-02 10:30:00",
                    "2026-08-02 10:30:00",
                ),
            ],
        )

        text, metadata = intelligence_views.brief_report(
            self.conn,
            hours=24,
            now=datetime(2026, 8, 2, 12, 0, 0),
        )

        self.assertEqual(metadata["opportunities"], 1)
        self.assertEqual(metadata["candidates"], 1)
        self.assertIn("## 已确认或正在推进", text)
        self.assertIn("## 待审核候选", text)
        self.assertIn("尚未确认，不等于真实商单", text)

    def test_home_report_exposes_five_intent_entries_and_triage_counts(self) -> None:
        self.insert(
            [message("品牌方A", "品牌方A", "2026-08-02 10:00:00", "方便确认报价吗？")]
        )
        self.conn.execute(
            """
            insert into opportunities(
                opportunity_key, chat, title, opportunity_type, stage, status, priority,
                last_signal_time, next_action, next_follow_up, created_at, updated_at
            ) values ('private:home', '品牌方A', '品牌合作', '商单', '待回复', 'active', 5,
                      '2026-08-02 10:00:00', '回复报价', '2026-08-02',
                      '2026-08-02 10:00:00', '2026-08-02 10:00:00')
            """
        )
        self.conn.execute(
            """
            insert into opportunities(
                opportunity_key, chat, title, opportunity_type, stage, status, priority,
                last_signal_time, next_action, created_at, updated_at
            ) values ('group:home', '资源群', '新合作', '商单', '新线索', 'new', 4,
                      '2026-08-02 11:00:00', '人工分流',
                      '2026-08-02 11:00:00', '2026-08-02 11:00:00')
            """
        )

        text, metadata = intelligence_views.home_report(
            self.conn,
            now=datetime(2026, 8, 2, 12, 0, 0),
        )

        self.assertEqual(metadata["open_opportunities"], 1)
        self.assertEqual(metadata["due_opportunities"], 1)
        self.assertEqual(metadata["inbox"], 1)
        self.assertIn("今日总览", text)
        self.assertIn("主题搜索", text)
        self.assertIn("联系人", text)
        self.assertIn("回复建议", text)
        self.assertIn("商单雷达", text)
        self.assertIn("立即处理", text)
        self.assertIn("仅供存档", text)

    def test_home_report_handles_timezone_aware_latest_message(self) -> None:
        self.insert(
            [message("品牌方A", "品牌方A", "2026-08-02T10:00:00+08:00", "方便确认报价吗？")]
        )

        text, metadata = intelligence_views.home_report(
            self.conn,
            now=datetime(2026, 8, 2, 4, 0, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(metadata["latest_message"], "2026-08-02T10:00:00+08:00")
        self.assertIn("约 2.0 小时前", text)

    def test_reply_report_uses_latest_context_and_never_claims_to_send(self) -> None:
        self.insert(
            [
                message("联系人C", "联系人C", "2026-08-01 09:00:00", "brief 发你了"),
                message("联系人C", "创作者A", "2026-08-01 09:10:00", "我明天整理初稿发你"),
                message("联系人C", "联系人C", "2026-08-02 10:00:00", "二稿修改好了吗？"),
            ]
        )
        self.conn.execute(
            """
            insert into opportunities(
                opportunity_key, chat, title, opportunity_type, stage, status, priority,
                last_signal_time, next_action, created_at, updated_at
            ) values ('private:reply', '联系人C', '项目A', '商单', '待品牌审核', 'active', 5,
                      '2026-08-02 10:00:00', '提交二稿',
                      '2026-08-02 10:00:00', '2026-08-02 10:00:00')
            """
        )

        text, metadata = intelligence_views.reply_report(self.conn, "联系人C", self_names=["创作者A"])

        self.assertEqual(metadata["intent"], "review")
        self.assertIn("二稿修改好了吗", text)
        self.assertIn("我明天整理初稿发你", text)
        self.assertIn("建议发", text)
        self.assertIn("不会发送", text)
        self.assertIn("按反馈修改", text)

    def test_reply_report_does_not_suggest_another_message_when_owner_sent_last(self) -> None:
        self.insert(
            [
                message("好友A", "好友A", "2026-08-02 10:00:00", "我建议你说话再精简一点"),
                message("好友A", "创作者A", "2026-08-02 10:02:00", "有道理，我改改"),
            ]
        )

        text, metadata = intelligence_views.reply_report(self.conn, "好友A", self_names=["创作者A"])

        self.assertTrue(metadata["owner_sent_last"])
        self.assertFalse(metadata["reply_needed"])
        self.assertIn("不用再回", text)

    def test_reply_report_matches_close_friend_tone_and_uses_one_draft(self) -> None:
        self.insert(
            [
                message("好友B", "创作者A", "2026-08-01 09:00:00", "bro一条视频2000元"),
                message("好友B", "好友B", "2026-08-02 10:00:00", "你这个是单个视频的价格吗"),
            ]
        )

        text, metadata = intelligence_views.reply_report(self.conn, "好友B", self_names=["创作者A"])

        self.assertEqual(metadata["relationship"], "亲近朋友")
        self.assertTrue(metadata["reply_needed"])
        self.assertIn("前面是单条价", text)
        self.assertEqual(text.count("## 建议发"), 1)
        self.assertNotIn("老师你好", text)

    def test_reply_report_learns_contact_specific_laugh_style_locally(self) -> None:
        self.insert(
            [
                message("好友C", "创作者A", "2026-08-01 09:00:00", "bro这个可以"),
                message("好友C", "创作者A", "2026-08-01 09:01:00", "hhh笑死"),
                message("好友C", "创作者A", "2026-08-01 09:02:00", "晚点看"),
                message("好友C", "创作者A", "2026-08-01 09:03:00", "好滴"),
                message("好友C", "创作者A", "2026-08-01 09:04:00", "我再改改"),
                message("好友C", "好友C", "2026-08-02 10:00:00", "建议你以后说得再短一点"),
            ]
        )

        text, metadata = intelligence_views.reply_report(self.conn, "好友C", self_names=["创作者A"])

        self.assertEqual(metadata["chat_style"]["preferred_laugh"], "hhh")
        self.assertIn("说人话点hhh", text)

    def test_reply_report_does_not_transfer_casual_style_to_sparse_business_chat(self) -> None:
        self.insert(
            [
                message("朋友D", "创作者A", "2026-08-01 08:00:00", "bro hhh"),
                message("客户D", "创作者A", "2026-08-01 09:00:00", "您好，材料已收到"),
                message("客户D", "客户D", "2026-08-02 10:00:00", "我建议这一版再精简一点"),
            ]
        )

        text, metadata = intelligence_views.reply_report(self.conn, "客户D", self_names=["创作者A"])

        self.assertEqual(metadata["relationship"], "商务联系人")
        self.assertNotIn("hhh", text)
        self.assertIn("会再精简一点", text)

    def test_contact_daily_keeps_priority_private_chats_and_reply_state(self) -> None:
        self.insert(
            [
                message("品牌联系人A", "创作者A", "2026-08-02 09:00:00", "方便发一下 brief 吗"),
                message("品牌联系人A", "品牌联系人A", "2026-08-02 10:00:00", "可以，先问下长文章报价多少？"),
                message("博主好友A", "博主好友A", "2026-08-02 09:30:00", "有个合作可以推荐你"),
                message("博主好友A", "创作者A", "2026-08-02 10:30:00", "好呀，麻烦帮我问问"),
                message("资讯账号", "资讯账号", "2026-08-02 11:00:00", "品牌推广案例合集"),
            ]
        )
        label_index = {
            "品牌联系人a": {"商单推广"},
            "博主好友a": {"自媒体网友"},
        }

        rows = radar.build_contact_daily_rows(
            self.conn,
            "2026-08-02 00:00:00",
            "2026-08-03 00:00:00",
            label_index,
            ["创作者A"],
        )
        by_chat = {row["联系人"]: row for row in rows}

        self.assertEqual(set(by_chat), {"品牌联系人A", "博主好友A"})
        self.assertEqual(by_chat["品牌联系人A"]["状态"], "待回复")
        self.assertIn("报价", by_chat["品牌联系人A"]["回复建议"])
        self.assertEqual(by_chat["博主好友A"]["状态"], "等待对方")

    def test_contact_daily_excludes_notifications_and_self_only_commercial_advice(self) -> None:
        self.insert(
            [
                radar.Message(
                    "服务通知",
                    "gh_test@app",
                    "2026-08-02 10:00:00",
                    "到站提醒",
                    "wechat-cli:timeline:notifymessage",
                ),
                message("朋友A", "创作者A", "2026-08-02 11:00:00", "我接商单开票应该怎么办？"),
                message("朋友A", "朋友A", "2026-08-02 11:01:00", "海南税率低一些"),
                message("品牌联系人B", "品牌联系人B", "2026-08-02 12:00:00", "想和你沟通商务合作"),
                message("品牌联系人B", "创作者A", "2026-08-02 12:01:00", "你好，可以聊聊"),
            ]
        )

        rows = radar.build_contact_daily_rows(
            self.conn,
            "2026-08-02 00:00:00",
            "2026-08-03 00:00:00",
            {},
            ["创作者A"],
        )
        names = {row["联系人"] for row in rows}

        self.assertNotIn("服务通知", names)
        self.assertNotIn("朋友A", names)
        self.assertIn("品牌联系人B", names)

    def test_contact_daily_surfaces_open_promise_from_before_current_window(self) -> None:
        self.insert(
            [
                message("品牌联系人C", "创作者A", "2026-08-01 18:00:00", "周四前可以给初稿"),
                message("品牌联系人C", "品牌联系人C", "2026-08-01 18:01:00", "好的老师，辛苦"),
                message("品牌联系人C", "品牌联系人C", "2026-08-02 09:00:00", "我们这边不开票"),
            ]
        )

        original_profile = radar.ACTIVE_PROFILE
        radar.ACTIVE_PROFILE = radar.merge_profile(
            radar.DEFAULT_PROFILE,
            {"labels": {"priority": ["重点客户"], "commercial": ["重点客户"]}},
        )
        try:
            rows = radar.build_contact_daily_rows(
                self.conn,
                "2026-08-02 00:00:00",
                "2026-08-03 00:00:00",
                {"品牌联系人c": {"重点客户"}},
                ["创作者A"],
            )
        finally:
            radar.ACTIVE_PROFILE = original_profile

        self.assertEqual(rows[0]["状态"], "待兑现")
        self.assertIn("初稿", rows[0]["历史承诺"]["内容"])

    def test_contact_daily_includes_two_way_custom_priority_topic_without_label(self) -> None:
        self.insert(
            [
                message("行业联系人", "行业联系人", "2026-08-02 10:00:00", "律所想找法律科技方案"),
                message("行业联系人", "创作者A", "2026-08-02 10:05:00", "可以，具体是什么场景？"),
                message("行业联系人", "行业联系人", "2026-08-02 10:08:00", "主要是合同审查"),
            ]
        )
        original_profile = radar.ACTIVE_PROFILE
        radar.ACTIVE_PROFILE = radar.merge_profile(
            radar.DEFAULT_PROFILE,
            {
                "intelligence_priorities": {
                    "focus_areas": ["法律科技"],
                    "priority_keywords": [],
                    "deprioritize_keywords": [],
                    "custom_topics": {"法律科技": ["律所", "合同审查"]},
                }
            },
        )
        try:
            rows = radar.build_contact_daily_rows(
                self.conn,
                "2026-08-02 00:00:00",
                "2026-08-03 00:00:00",
                {},
                ["创作者A"],
            )
        finally:
            radar.ACTIVE_PROFILE = original_profile

        self.assertEqual(rows[0]["联系人"], "行业联系人")
        self.assertEqual(rows[0]["角色"], "个人重点主题联系人")
        self.assertGreater(rows[0]["个人重点消息数"], 0)

    def test_contact_daily_priority_labels_only_excludes_unlabelled_contacts(self) -> None:
        self.insert(
            [
                message("品牌联系人", "品牌联系人", "2026-08-02 10:00:00", "这轮预算可以聊聊"),
                message("品牌联系人", "创作者A", "2026-08-02 10:05:00", "可以，发我 brief 看看"),
                message("未标签联系人", "未标签联系人", "2026-08-02 11:00:00", "有个付费合作想找你"),
                message("未标签联系人", "创作者A", "2026-08-02 11:05:00", "可以聊聊"),
            ]
        )
        original_profile = radar.ACTIVE_PROFILE
        radar.ACTIVE_PROFILE = radar.merge_profile(
            radar.DEFAULT_PROFILE,
            {
                "labels": {"priority": ["商单推广"], "commercial": ["商单推广"]},
                "contact_daily": {"scope": "priority_labels_only"},
            },
        )
        try:
            rows = radar.build_contact_daily_rows(
                self.conn,
                "2026-08-02 00:00:00",
                "2026-08-03 00:00:00",
                {"品牌联系人": {"商单推广"}},
                ["创作者A"],
            )
        finally:
            radar.ACTIVE_PROFILE = original_profile

        self.assertEqual([row["联系人"] for row in rows], ["品牌联系人"])


if __name__ == "__main__":
    unittest.main()

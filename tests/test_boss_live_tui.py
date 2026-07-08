import argparse
import itertools
import importlib.util
import json
import pathlib
import sys
import tomllib
import unittest
from unittest import mock


ROOT_PATH = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT_PATH / "scripts" / "boss_live_tui.py"
PYPROJECT_PATH = ROOT_PATH / "pyproject.toml"


def load_module():
    sys.modules.setdefault("websocket", mock.Mock())
    sys.modules.setdefault("requests", mock.Mock())
    spec = importlib.util.spec_from_file_location("boss_live_tui", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BossLiveTUITests(unittest.TestCase):
    def test_script_exists_and_has_no_offline_input_mode(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("Realtime terminal UI", text)
        self.assertNotIn("add_argument(\"--input\"", text)
        self.assertNotIn("boss_jobs_", text)
        self.assertIn("never reads local result files", text)

    def test_arg_parser_keeps_live_search_arguments(self):
        module = load_module()

        args = module.build_arg_parser().parse_args([
            "--keyword", "AI Agent",
            "--city", "上海",
            "--salary", "406",
        ])

        self.assertEqual(args.keyword, "AI Agent")
        self.assertEqual(args.city, "上海")
        self.assertEqual(module.build_filters(args), {"salary": "406"})
        self.assertFalse(args.auto_detail)

    def test_auto_detail_is_opt_in(self):
        module = load_module()

        default_args = module.build_arg_parser().parse_args([])
        enabled_args = module.build_arg_parser().parse_args(["--auto-detail"])

        self.assertFalse(default_args.auto_detail)
        self.assertTrue(enabled_args.auto_detail)

    def test_messages_auto_open_selected_conversation(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("open_selected_conversation_if_needed", text)
        self.assertIn("self.open_selected_conversation_if_needed()", text)
        self.assertNotIn("按 Enter 打开聊天记录", text)
        self.assertIn("选中自动打开", text)
        self.assertNotIn("Enter 打开会话", text)
        self.assertNotIn("正在等待聊天记录", text)
        self.assertIn("聊天记录会自动加载", text)

    def test_chat_switch_clears_stale_content_and_ignores_old_results(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("self.chat = {}", text)
        self.assertIn("self.chat_request_seq += 1", text)
        self.assertIn("request_seq != self.chat_request_seq", text)
        self.assertIn("conv_id and conv_id != self.selected_conversation_id", text)
        self.assertIn("正在加载聊天记录", text)
        self.assertIn("•  •  •", text)

    def test_chat_send_is_bound_to_submitted_conversation(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("self.chat_operation_lock = threading.RLock()", text)
        self.assertIn("self.send_message(message, dict(conv), self.chat_request_seq)", text)
        self.assertIn("with self.chat_operation_lock:", text)
        self.assertIn("self.client.open_conversation(conversation.get(\"index\", 0))", text)
        self.assertIn("result = self.client.send_current_message(message)", text)
        self.assertIn("sent_conv_id and sent_conv_id == self.selected_conversation_id", text)
        self.assertNotIn("self.send_message(message)\n", text)

    def test_chat_messages_are_realtime_without_local_cache(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertNotIn("CHAT_CACHE_TTL_SECONDS", text)
        self.assertNotIn("CONVERSATION_SQLITE_CACHE_TTL_SECONDS", text)
        self.assertNotIn("CHAT_SQLITE_CACHE_TTL_SECONDS", text)
        self.assertNotIn("conversation_cache", text)
        self.assertNotIn("chat_cache", text)
        self.assertNotIn("get_cached_chat", text)
        self.assertNotIn("put_cached_chat", text)
        self.assertNotIn("会话缓存命中", text)
        self.assertIn("消息列表已实时刷新", text)

    def test_chat_open_uses_ready_polling_and_debounce(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("CHAT_PAGE_READY_JS", text)
        self.assertIn("CURRENT_CHAT_READY_JS", text)
        self.assertIn("def _wait_until_js_ready", text)
        self.assertIn("CONVERSATION_OPEN_DEBOUNCE_SECONDS = 0.25", text)
        self.assertIn("def schedule_open_conversation", text)
        self.assertIn("open-conversation-debounce", text)
        self.assertIn("open_conversation_schedule_seq", text)
        self.assertIn("schedule_seq != self.open_conversation_schedule_seq", text)
        self.assertNotIn("time.sleep(4)", text)
        self.assertNotIn("time.sleep(3)", text)

    def test_keyboard_can_toggle_focus_between_list_and_detail(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("(\"tab\", \"toggle_focus\", \"切焦点\")", text)
        self.assertIn("def focus_list_panel(self):", text)
        self.assertIn("def focus_detail_panel(self):", text)
        self.assertIn("def action_toggle_focus(self):", text)
        self.assertIn("Tab 切焦点", text)
        self.assertIn("table.focus()", text)
        self.assertIn("#preview_scroll", text)
        self.assertNotIn("if chat.get(\"canReply\"):\n                self.query_one(\"#reply_input\", Input).focus()", text)

    def test_risk_page_detection_catches_captcha_text(self):
        module = load_module()

        self.assertTrue(module.looks_like_risk_page("请完成验证码安全验证"))
        self.assertTrue(module.looks_like_risk_page("访问异常，请拖动滑块"))
        self.assertFalse(module.looks_like_risk_page("负责 Agent 平台建设"))

    def test_fetch_page_uses_live_cdp_api(self):
        module = load_module()
        fake_cdp = mock.Mock()
        fake_cdp.send.side_effect = [
            {"result": {"targetId": "target-1"}},
            {"result": {"sessionId": "sid-1"}},
        ]
        fake_cdp.eval_js.return_value = json.dumps([{
            "title": "AI Agent工程师",
            "salary": "30-60K",
            "salary_source": "api",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }])

        with mock.patch.object(module.boss, "CDPSession", return_value=fake_cdp), \
                mock.patch.object(module.time, "sleep", return_value=None):
            client = module.LiveBossClient("AI Agent", "上海", {"salary": "406"}, cdp_port=9333)
            jobs = client.fetch_page(2)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["_live_page"], 2)
        eval_js = fake_cdp.eval_js.call_args.args[0]
        self.assertIn("query=AI+Agent", eval_js)
        self.assertIn("city=101020100", eval_js)
        self.assertIn("page=2", eval_js)
        self.assertIn("salary=406", eval_js)
        create_target_params = fake_cdp.send.call_args_list[0].args[1]
        self.assertTrue(create_target_params["background"])

    def test_message_client_parses_conversations(self):
        module = load_module()
        fake_cdp = mock.Mock()
        fake_cdp.send.side_effect = [
            {"result": {"targetId": "target-1"}},
            {"result": {"sessionId": "sid-1"}},
            {"result": {"targetId": "chat-1"}},
            {"result": {"sessionId": "chat-sid"}},
        ]
        fake_cdp.eval_js.return_value = json.dumps([{
            "index": 0,
            "title": "方先生 神州信息",
            "preview": "您正在与Boss方先生沟通",
            "text": "06月26日 方先生神州信息招聘主管 您正在与Boss方先生沟通",
            "unread": True,
        }])

        with mock.patch.object(module.boss, "CDPSession", return_value=fake_cdp), \
                mock.patch.object(module.time, "sleep", return_value=None):
            client = module.LiveBossClient("AI Agent", "上海", cdp_port=9333)
            conversations = client.fetch_conversations()

        self.assertEqual(len(conversations), 1)
        self.assertTrue(conversations[0]["unread"])
        self.assertIn("conversation_id", conversations[0])
        self.assertIn(".chat-content li", fake_cdp.eval_js.call_args.args[0])

    def test_conversation_id_is_stable_when_preview_changes(self):
        module = load_module()
        first = module.normalize_conversation({
            "index": 0,
            "title": "陈先生 bilibili招聘专员",
            "preview": "已读 1",
            "text": "陈先生 bilibili招聘专员 已读 1",
        })
        second = module.normalize_conversation({
            "index": 0,
            "title": "陈先生 bilibili招聘专员",
            "preview": "您好，方便沟通吗",
            "text": "陈先生 bilibili招聘专员 您好，方便沟通吗",
        })

        self.assertEqual(module.stable_conversation_identity(first), module.stable_conversation_identity(second))

    def test_empty_realtime_chat_does_not_erase_visible_history(self):
        module = load_module()
        current = {
            "canReply": True,
            "messages": [
                {"direction": "boss", "time": "15:30", "text": "你好，可以发一下简历吗？"},
                {"direction": "me", "time": "15:31", "text": "可以，我稍后发您。"},
            ],
        }
        incoming = {"canReply": True, "messages": [], "rawText": "陈先生 发送"}

        merged = module.merge_chat_without_losing_history(current, incoming)

        self.assertEqual(len(merged["messages"]), 2)
        self.assertTrue(merged["_preserved_messages"])
        self.assertEqual(merged["rawText"], "陈先生 发送")

    def test_send_current_message_uses_explicit_text(self):
        module = load_module()
        fake_cdp = mock.Mock()
        fake_cdp.send.side_effect = itertools.chain([
            {"result": {"targetId": "target-1"}},
            {"result": {"sessionId": "sid-1"}},
            {"result": {"targetId": "chat-1"}},
            {"result": {"sessionId": "chat-sid"}},
        ], itertools.repeat({}))
        fake_cdp.eval_js.side_effect = [
            json.dumps({"ready": True, "rows": 1}),
            json.dumps({"ready": True, "inputTag": "DIV", "inputClass": "chat-input"}),
            json.dumps({"ready": True, "inputText": "您好，我想进一步沟通这个岗位。", "x": 320, "y": 600}),
            json.dumps({"clicked": True, "text": "发送"}),
            json.dumps({"sent": True, "verified": True, "inputText": ""}),
        ]

        with mock.patch.object(module.boss, "CDPSession", return_value=fake_cdp), \
                mock.patch.object(module.time, "sleep", return_value=None):
            client = module.LiveBossClient("AI Agent", "上海", cdp_port=9333)
            result = client.send_current_message("您好，我想进一步沟通这个岗位。")

        self.assertTrue(result["sent"])
        self.assertTrue(result["verified"])
        inserted = [
            call for call in fake_cdp.send.call_args_list
            if call.args[0] == "Input.insertText"
        ][0]
        self.assertEqual(inserted.args[1]["text"], "您好，我想进一步沟通这个岗位。")
        sent_js = fake_cdp.eval_js.call_args_list[2].args[0]
        self.assertIn("您好，我想进一步沟通这个岗位。", sent_js)
        self.assertIn(".chat-input", sent_js)

    def test_fetch_detail_does_not_open_foreground_tab_when_target_id_missing(self):
        module = load_module()
        fake_cdp = mock.Mock()
        fake_cdp.send.side_effect = [
            {"result": {"targetId": "target-1"}},
            {"result": {"sessionId": "sid-1"}},
            {"id": 20, "result": {"success": True}},
        ]
        job = {
            "job_id": "abc123",
            "title": "AI Agent工程师",
            "salary": "30-60K",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }

        with mock.patch.object(module.boss, "CDPSession", return_value=fake_cdp), \
                mock.patch.object(module.time, "sleep", return_value=None):
            client = module.LiveBossClient("AI Agent", "上海", cdp_port=9333)
            with self.assertRaisesRegex(RuntimeError, "创建浏览器标签失败"):
                client.fetch_detail(job)

        self.assertTrue(fake_cdp.send.call_args_list[2].args[1]["background"])
        self.assertEqual(len(fake_cdp.send.call_args_list), 3)

    def test_fetch_detail_uses_in_memory_cache_within_ttl(self):
        module = load_module()
        fake_cdp = mock.Mock()
        fake_cdp.send.side_effect = itertools.chain([
            {"result": {"targetId": "target-1"}},
            {"result": {"sessionId": "sid-1"}},
            {"result": {"targetId": "detail-1"}},
            {"result": {"sessionId": "detail-sid"}},
        ], itertools.repeat({}))
        fake_cdp.eval_js.return_value = json.dumps({"jd": "完整 JD", "tags": ["Python"]})
        job = {
            "job_id": "abc123",
            "title": "AI Agent工程师",
            "salary": "30-60K",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }

        with mock.patch.object(module.boss, "CDPSession", return_value=fake_cdp), \
                mock.patch.object(module.time, "sleep", return_value=None), \
                mock.patch.object(module.time, "time", side_effect=[1000, 1000, 1000, 1005]):
            client = module.LiveBossClient("AI Agent", "上海", cdp_port=9333)
            first = client.fetch_detail(job)
            second = client.fetch_detail(job)

        self.assertEqual(first["jd"], "完整 JD")
        self.assertEqual(second["jd"], "完整 JD")
        self.assertTrue(second["_cache_hit"])
        create_calls = [
            call for call in fake_cdp.send.call_args_list
            if call.args[0] == "Target.createTarget"
        ]
        self.assertEqual(len(create_calls), 2)

    def test_fetch_detail_refreshes_after_cache_ttl(self):
        module = load_module()
        fake_cdp = mock.Mock()
        fake_cdp.send.side_effect = itertools.chain([
            {"result": {"targetId": "target-1"}},
            {"result": {"sessionId": "sid-1"}},
            {"result": {"targetId": "detail-1"}},
            {"result": {"sessionId": "detail-sid"}},
            {},
            {},
            {"result": {"targetId": "detail-2"}},
            {"result": {"sessionId": "detail-sid-2"}},
            {},
        ], itertools.repeat({}))
        fake_cdp.eval_js.side_effect = [
            json.dumps({"jd": "旧 JD", "tags": ["Python"]}),
            json.dumps({"jd": "新 JD", "tags": ["Go"]}),
        ]
        job = {
            "job_id": "abc123",
            "title": "AI Agent工程师",
            "salary": "30-60K",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }

        with mock.patch.object(module.boss, "CDPSession", return_value=fake_cdp), \
                mock.patch.object(module.time, "sleep", return_value=None), \
                mock.patch.object(module.time, "time", side_effect=[1000, 1000, 1000, 4000, 4000, 4000, 4000]):
            client = module.LiveBossClient("AI Agent", "上海", cdp_port=9333)
            first = client.fetch_detail(job)
            second = client.fetch_detail(job)

        self.assertEqual(first["jd"], "旧 JD")
        self.assertEqual(second["jd"], "新 JD")
        self.assertNotIn("_cache_hit", second)
        create_calls = [
            call for call in fake_cdp.send.call_args_list
            if call.args[0] == "Target.createTarget"
        ]
        self.assertEqual(len(create_calls), 3)

    def test_send_current_message_falls_back_to_enter_when_button_disabled(self):
        module = load_module()
        fake_cdp = mock.Mock()
        fake_cdp.send.side_effect = itertools.chain([
            {"result": {"targetId": "target-1"}},
            {"result": {"sessionId": "sid-1"}},
            {"result": {"targetId": "chat-1"}},
            {"result": {"sessionId": "chat-sid"}},
        ], itertools.repeat({}))
        fake_cdp.eval_js.side_effect = [
            json.dumps({"ready": True, "rows": 1}),
            json.dumps({"ready": True, "inputTag": "DIV", "inputClass": "chat-input"}),
            json.dumps({"ready": False, "reason": "send button disabled or not found", "inputText": "您好"}),
            json.dumps({"sent": True, "verified": True, "inputText": ""}),
        ]

        with mock.patch.object(module.boss, "CDPSession", return_value=fake_cdp), \
                mock.patch.object(module.time, "sleep", return_value=None):
            client = module.LiveBossClient("AI Agent", "上海", cdp_port=9333)
            result = client.send_current_message("您好")

        self.assertTrue(result["sent"])
        self.assertEqual(result["method"], "enter")
        key_events = [
            call for call in fake_cdp.send.call_args_list
            if call.args[0] == "Input.dispatchKeyEvent"
        ]
        self.assertEqual(len(key_events), 2)

    def test_format_send_failure_includes_debug_context(self):
        module = load_module()
        result = {
            "reason": "send button disabled or not found",
            "debug": {
                "inputText": "您好，想了解一下岗位",
                "buttons": [
                    {"text": "常用语", "className": "btn"},
                    {"text": "", "className": "btn-send disabled"},
                ],
            },
        }

        message = module.format_send_failure(result)

        self.assertIn("send button disabled", message)
        self.assertIn("输入框已有", message)
        self.assertIn("常用语", message)

    def test_sent_message_is_appended_locally_when_dom_is_stale(self):
        module = load_module()
        stale_chat = {
            "messages": [
                {"direction": "boss", "time": "10:00", "text": "您好"},
            ]
        }

        chat = module.append_local_message(stale_chat, "我想了解一下岗位。")
        chat = module.append_local_message(chat, "我想了解一下岗位。")

        self.assertEqual(len(chat["messages"]), 2)
        self.assertEqual(chat["messages"][-1]["direction"], "me")
        self.assertEqual(chat["messages"][-1]["time"], "刚刚")
        self.assertEqual(chat["messages"][-1]["text"], "我想了解一下岗位。")

    def test_conversation_preview_updates_after_send(self):
        module = load_module()
        conversation = {
            "title": "李女士 某科技公司",
            "preview": "您好",
            "text": "李女士 某科技公司 您好",
            "unread": True,
        }

        module.update_conversation_preview(conversation, "方便发一下 JD 吗？")

        self.assertEqual(conversation["preview"], "我: 方便发一下 JD 吗？")
        self.assertFalse(conversation["unread"])
        self.assertIn("我: 方便发一下 JD 吗？", conversation["text"])

    def test_conversation_delivery_status_is_split_from_preview(self):
        module = load_module()

        conversation = module.normalize_conversation({
            "index": 0,
            "text": "15:38 高圆圆拓邦人才猎头顾问 [已读] TUI测试",
            "title": "15:38 高圆圆拓邦人才猎头顾问 [已",
            "preview": "读] TUI测试",
            "unread": False,
        })

        self.assertEqual(conversation["title"], "高圆圆拓邦人才猎头顾问")
        self.assertEqual(conversation["preview"], "TUI测试")
        self.assertEqual(conversation["delivery_status"], "已读")
        self.assertEqual(conversation["status_label"], "已读")

    def test_conversation_unread_status_stays_visible(self):
        module = load_module()

        conversation = module.normalize_conversation({
            "index": 0,
            "text": "昨天 王女士某公司 您好",
            "title": "昨天 王女士某公司",
            "preview": "您好",
            "unread": True,
        })

        self.assertEqual(conversation["title"], "王女士某公司")
        self.assertEqual(conversation["status_label"], "● 未读")

    def test_normalize_chat_moves_outgoing_status_out_of_body(self):
        module = load_module()

        chat = module.normalize_chat({
            "messages": [
                {"direction": "me", "time": "15:38", "text": "已读 TUI测试"},
                {"direction": "boss", "time": "15:39", "text": "您好"},
            ]
        })

        self.assertEqual(chat["messages"][0]["status"], "已读")
        self.assertEqual(chat["messages"][0]["text"], "TUI测试")
        self.assertEqual(chat["messages"][1]["text"], "您好")

    def test_normalize_chat_filters_boss_system_cards(self):
        module = load_module()

        chat = module.normalize_chat({
            "messages": [
                {"direction": "boss", "text": "你与该职位竞争者PK情况 共人投递，你超过竞争者 查看详细分析"},
                {"direction": "boss", "text": "方便发一份简历吗？"},
            ]
        })

        self.assertEqual(len(chat["messages"]), 1)
        self.assertEqual(chat["messages"][0]["text"], "方便发一份简历吗？")

    def test_normalize_chat_treats_delivery_prefixed_duplicate_as_outgoing(self):
        module = load_module()

        chat = module.normalize_chat({
            "messages": [
                {"direction": "boss", "time": "15:54", "text": "已读 贵司的AI Agent开发岗位还在招人么？"},
                {"direction": "me", "time": "15:54", "text": "已读 贵司的AI Agent开发岗位还在招人么？"},
            ]
        })

        self.assertEqual(len(chat["messages"]), 1)
        self.assertEqual(chat["messages"][0]["direction"], "me")
        self.assertEqual(chat["messages"][0]["status"], "已读")
        self.assertEqual(chat["messages"][0]["text"], "贵司的AI Agent开发岗位还在招人么？")

    def test_chat_empty_state_uses_conversation_preview(self):
        module = load_module()

        rendered = module.format_chat_text(
            {"title": "陈先生bilibili招聘专员", "preview": "已读 您好，我对bilibili的AI Agent开发很感兴趣"},
            {"canReply": True, "messages": []},
        )

        self.assertIn("最近消息", rendered)
        self.assertIn("bilibili的AI\u00a0Agent", rendered)
        self.assertNotIn("当前会话暂无可读消息", rendered)

    def test_chat_body_protects_short_english_terms_from_bad_wraps(self):
        module = load_module()

        rendered = module.chat_body_text("贵司的AI Agent全栈工程师岗位还在招人么？")

        self.assertIn("AI\u00a0Agent", rendered)
        self.assertNotIn("AI Agent", rendered)

    def test_chat_render_uses_distinct_roles_and_hint_styles(self):
        module = load_module()

        rendered = module.format_chat_text(
            {"title": "陈先生", "preview": "您好"},
            {
                "canReply": True,
                "messages": [
                    {"direction": "boss", "time": "15:30", "text": "方便发一份简历吗？"},
                    {"direction": "me", "time": "15:31", "status": "送达", "text": "贵司的AI Agent岗位还在招人么？"},
                ],
            },
        )

        self.assertIn(f"[bold {module.COLOR_ACCENT}]Boss[/]", rendered)
        self.assertIn(f"[bold {module.COLOR_GREEN}]我[/]", rendered)
        self.assertIn(module.COLOR_BOSS_BODY, rendered)
        self.assertIn(module.COLOR_ME_BODY, rendered)
        self.assertIn(module.COLOR_HINT, rendered)
        self.assertIn("AI\u00a0Agent", rendered)

    def test_chat_fetch_js_has_fallback_message_parsing(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("function fallbackMessages", text)
        self.assertIn("[class*=\"message-item\"]", text)
        self.assertIn("rawText", text)
        self.assertIn("fallbackLines.length > 1", text)

    def test_start_greeting_clicks_startchat_button_after_detail_load(self):
        module = load_module()
        fake_cdp = mock.Mock()
        fake_cdp.send.side_effect = itertools.chain([
            {"result": {"targetId": "target-1"}},
            {"result": {"sessionId": "sid-1"}},
            {"result": {"targetId": "detail-1"}},
            {"result": {"sessionId": "detail-sid"}},
        ], itertools.repeat({}))
        fake_cdp.eval_js.return_value = json.dumps({
            "ready": True,
            "text": "立即沟通",
            "x": 200,
            "y": 160,
        })
        job = {
            "title": "AI Agent工程师",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }

        with mock.patch.object(module.boss, "CDPSession", return_value=fake_cdp), \
                mock.patch.object(module.time, "sleep", return_value=None):
            client = module.LiveBossClient("AI Agent", "上海", cdp_port=9333)
            result = client.start_greeting(job)

        self.assertTrue(result["sent"])
        self.assertEqual(result["button"], "立即沟通")
        self.assertIn(".btn-startchat", fake_cdp.eval_js.call_args.args[0])

    def test_default_greeting_message_uses_job_context(self):
        module = load_module()
        message = module.default_greeting_message({
            "title": "AI Agent工程师",
            "boss_name": "甲公司",
        })

        self.assertIn("甲公司的AI Agent工程师", message)
        self.assertIn("方便沟通", message)

    def test_start_greeting_can_send_custom_message_after_click(self):
        module = load_module()
        fake_cdp = mock.Mock()
        fake_cdp.send.side_effect = itertools.chain([
            {"result": {"targetId": "target-1"}},
            {"result": {"sessionId": "sid-1"}},
            {"result": {"targetId": "detail-1"}},
            {"result": {"sessionId": "detail-sid"}},
        ], itertools.repeat({}))
        fake_cdp.eval_js.side_effect = [
            json.dumps({"ready": True, "text": "立即沟通", "x": 200, "y": 160}),
            json.dumps({"ready": True, "inputTag": "DIV", "inputClass": "chat-input"}),
            json.dumps({"ready": True, "inputTag": "DIV", "inputClass": "chat-input"}),
            json.dumps({"ready": True, "inputText": "您好，想了解一下这个岗位。", "x": 320, "y": 600}),
            json.dumps({"clicked": True, "text": "发送"}),
            json.dumps({"sent": True, "verified": True, "inputText": ""}),
        ]
        job = {
            "title": "AI Agent工程师",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }

        with mock.patch.object(module.boss, "CDPSession", return_value=fake_cdp), \
                mock.patch.object(module.time, "sleep", return_value=None):
            client = module.LiveBossClient("AI Agent", "上海", cdp_port=9333)
            result = client.start_greeting(job, "您好，想了解一下这个岗位。")

        self.assertTrue(result["sent"])
        self.assertTrue(result["custom_message_sent"])
        self.assertIn("您好，想了解一下这个岗位。", fake_cdp.eval_js.call_args_list[-1].args[0])

    def test_start_greeting_dom_click_does_not_require_coordinates(self):
        module = load_module()
        fake_cdp = mock.Mock()
        fake_cdp.send.side_effect = itertools.chain([
            {"result": {"targetId": "target-1"}},
            {"result": {"sessionId": "sid-1"}},
            {"result": {"targetId": "detail-1"}},
            {"result": {"sessionId": "detail-sid"}},
        ], itertools.repeat({}))
        fake_cdp.eval_js.return_value = json.dumps({
            "ready": True,
            "clicked": True,
            "text": "立即沟通",
        })
        job = {
            "title": "AI Agent工程师",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }

        with mock.patch.object(module.boss, "CDPSession", return_value=fake_cdp), \
                mock.patch.object(module.time, "sleep", return_value=None):
            client = module.LiveBossClient("AI Agent", "上海", cdp_port=9333)
            result = client.start_greeting(job)

        self.assertTrue(result["sent"])
        self.assertEqual(result["button"], "立即沟通")

    def test_send_current_message_forces_input_when_insert_text_does_not_update_dom(self):
        module = load_module()
        fake_cdp = mock.Mock()
        fake_cdp.send.side_effect = itertools.chain([
            {"result": {"targetId": "target-1"}},
            {"result": {"sessionId": "sid-1"}},
            {"result": {"targetId": "chat-1"}},
            {"result": {"sessionId": "chat-sid"}},
        ], itertools.repeat({}))
        fake_cdp.eval_js.side_effect = [
            json.dumps({"ready": True, "rows": 1}),
            json.dumps({"ready": True, "inputTag": "DIV", "inputClass": "chat-input"}),
            json.dumps({"ready": False, "reason": "send button disabled or not found", "inputText": ""}),
            json.dumps({"ready": True, "inputText": "您好"}),
            json.dumps({"ready": True, "inputText": "", "x": 320, "y": 600}),
            json.dumps({"clicked": True, "text": "发送"}),
            json.dumps({"sent": True, "verified": True, "inputText": ""}),
        ]

        with mock.patch.object(module.boss, "CDPSession", return_value=fake_cdp), \
                mock.patch.object(module.time, "sleep", return_value=None):
            client = module.LiveBossClient("AI Agent", "上海", cdp_port=9333)
            result = client.send_current_message("您好")

        self.assertTrue(result["sent"])
        self.assertEqual(result["method"], "button")
        self.assertIn("expected = \"您好\"", fake_cdp.eval_js.call_args_list[3].args[0])

    def test_agent_context_is_json_serializable_and_ui_agnostic(self):
        module = load_module()
        job = {
            "job_id": "abc123",
            "_live_page": 2,
            "title": "AI Agent工程师",
            "salary": "30-60K",
            "boss_name": "甲公司",
            "location": "上海·浦东新区",
            "tags": "3-5年 | 本科",
            "skills": "Python | LLM",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }
        detail = {"skill_tags": ["Python", "LLM"], "jd": "负责 AI Agent 平台"}

        context = module.build_agent_context("AI Agent", "上海", {"salary": "406"}, job, detail)

        json.dumps(context, ensure_ascii=False)
        self.assertEqual(context["source"]["mode"], "live_cdp")
        self.assertEqual(context["source"]["page"], 2)
        self.assertEqual(context["job"]["title"], "AI Agent工程师")
        self.assertTrue(context["detail"]["loaded"])
        self.assertNotIn("client", context)
        self.assertNotIn("widget", json.dumps(context, ensure_ascii=False).lower())

    def test_clean_detail_jd_keeps_only_job_description_segment(self):
        module = load_module()
        raw = "\n".join([
            "微信扫码分享",
            "举报",
            "职位描述",
            "岗位职责",
            "1、负责 AI Agent 平台建设",
            "2、熟悉 LangChain/LangGraph",
            "招聘者",
            "个人综合排名：在人中排名第",
            "BOSS 安全提示",
            "公司介绍",
        ])

        cleaned = module.clean_detail_jd(raw)

        self.assertIn("负责 AI Agent 平台建设", cleaned)
        self.assertIn("LangChain", cleaned)
        self.assertNotIn("微信扫码分享", cleaned)
        self.assertNotIn("招聘者", cleaned)
        self.assertNotIn("BOSS 安全提示", cleaned)
        self.assertNotIn("公司介绍", cleaned)

    def test_clean_detail_jd_removes_recruiter_tail_without_truncating_jd(self):
        module = load_module()
        raw = "\n".join([
            "职位描述",
            "岗位职责",
            "1、负责 Agent 系统建设",
            "2、熟悉 HR SaaS 业务优先",
            "陈先生",
            "刚刚活跃",
            "bilibili",
            "招聘专员",
        ])

        cleaned = module.clean_detail_jd(raw)

        self.assertIn("负责 Agent 系统建设", cleaned)
        self.assertIn("熟悉 HR SaaS 业务优先", cleaned)
        self.assertNotIn("陈先生", cleaned)
        self.assertNotIn("刚刚活跃", cleaned)
        self.assertNotIn("招聘专员", cleaned)

    def test_clean_detail_jd_removes_plain_recruiter_name_before_activity_tail(self):
        module = load_module()
        raw = "\n".join([
            "职位描述",
            "任职要求",
            "4、扎实的软件工程师能力，和逻辑能力，可以把想法变成可实际落地的项目",
            "周近",
            "刚刚活跃",
            "招聘专员",
        ])

        cleaned = module.clean_detail_jd(raw)

        self.assertIn("扎实的软件工程师能力", cleaned)
        self.assertNotIn("周近", cleaned)
        self.assertNotIn("刚刚活跃", cleaned)
        self.assertNotIn("招聘专员", cleaned)

    def test_online_recruiter_tail_is_separated_from_jd(self):
        module = load_module()
        raw = "\n".join([
            "职位描述",
            "任职要求",
            "3.长期关注 AI 前沿技术，有高质量开源项目贡献者优先",
            "周女士",
            "在线",
            "上海珀懿电商",
        ])

        cleaned = module.clean_detail_jd(raw)
        recruiter = module.format_recruiter_info(raw)

        self.assertIn("长期关注 AI 前沿技术", cleaned)
        self.assertNotIn("周女士", cleaned)
        self.assertNotIn("在线", cleaned)
        self.assertNotIn("上海珀懿电商", cleaned)
        self.assertEqual(recruiter, "周女士 · 在线 · 上海珀懿电商")

    def test_recent_active_recruiter_tail_is_separated_from_jd(self):
        module = load_module()
        raw = "\n".join([
            "职位描述",
            "任职要求",
            "5.沟通与分享意识：乐于交流技术方案与经验，具备跨角色协作能力。",
            "冯先生",
            "3日内活跃",
            "云深网络",
            ".",
            "HR经理",
        ])

        cleaned = module.clean_detail_jd(raw)
        recruiter = module.format_recruiter_info(raw)

        self.assertIn("沟通与分享意识", cleaned)
        self.assertNotIn("冯先生", cleaned)
        self.assertNotIn("3日内活跃", cleaned)
        self.assertNotIn("云深网络", cleaned)
        self.assertNotIn("HR经理", cleaned)
        self.assertEqual(recruiter, "冯先生 · 3日内活跃 · 云深网络 · HR经理")

    def test_preview_text_is_structured_for_detail_panel(self):
        module = load_module()
        job = {
            "title": "AI Agent开发工程师",
            "salary": "25-50K",
            "boss_name": "途游游戏",
            "company_scale": "1000-9999人",
            "company_industry": "游戏",
            "location": "上海·浦东新区·上南",
            "tags": "1-3年 | 本科",
            "skills": "Python | LangGraph",
            "welfare": "年终奖 | 五险一金",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }
        detail = {"skill_tags": ["Agent"], "jd": "职位描述\n负责 AI Agent 平台\n招聘者\n李女士"}

        preview = module.format_preview_text(job, detail)

        self.assertIn("岗位概览", preview)
        self.assertIn("技能", preview)
        self.assertIn("福利", preview)
        self.assertIn("JD 正文", preview)
        self.assertIn("负责 AI Agent 平台", preview)
        self.assertNotIn("招聘者", preview)

    def test_preview_hides_recruiter_info_and_only_keeps_open_action(self):
        module = load_module()
        job = {
            "title": "AI Agent开发工程师",
            "salary": "25-50K",
            "boss_name": "甲公司",
            "location": "上海",
            "tags": "3-5年 | 本科",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }
        detail = {"jd": "职位描述\n负责 Agent 平台\n周女士\n在线\n上海珀懿电商"}

        preview = module.format_preview_text(job, detail)

        self.assertIn("o 打开网页", preview)
        self.assertNotIn("页面信息", preview)
        self.assertNotIn("联系人", preview)
        self.assertNotIn("周女士 · 在线 · 上海珀懿电商", preview)
        self.assertNotIn("• 周女士", preview)
        self.assertNotIn("• 在线", preview)

    def test_preview_only_shows_open_action_not_link_text(self):
        module = load_module()
        long_url = "https://www.zhipin.com/job_detail/ff9773e643b25d950nF40t61GVJR.html"
        job = {
            "title": "AI Agent开发工程师",
            "salary": "25-50K",
            "boss_name": "甲公司",
            "location": "上海",
            "tags": "3-5年 | 本科",
            "job_link": long_url,
        }

        preview = module.format_preview_text(job, {"jd": "职位描述\n负责 Agent 平台"})

        self.assertIn("o 打开网页", preview)
        self.assertNotIn("页面信息", preview)
        self.assertNotIn("联系人", preview)
        self.assertNotIn("链接", preview)
        self.assertNotIn("job_detail/", preview)
        self.assertNotIn("复制完整链接", preview)
        self.assertNotIn(long_url, preview)

    def test_preview_default_placeholder_asks_for_enter(self):
        module = load_module()
        job = {
            "title": "AI Agent开发工程师",
            "salary": "25-50K",
            "boss_name": "甲公司",
            "location": "上海",
            "tags": "3-5年 | 本科",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }

        preview = module.format_preview_text(job, None)

        self.assertIn("按 Enter 打开详情页", preview)
        self.assertNotIn("•  •  •", preview)
        self.assertNotIn("正在自动加载详情页", preview)

    def test_preview_auto_detail_loading_state_is_explicit(self):
        module = load_module()
        job = {
            "title": "AI Agent开发工程师",
            "salary": "25-50K",
            "boss_name": "甲公司",
            "location": "上海",
            "tags": "3-5年 | 本科",
            "job_link": "https://www.zhipin.com/job_detail/abc.html",
        }

        preview = module.format_preview_text(job, None, auto_detail=True, loading=True)

        self.assertIn("正在自动加载详情页", preview)
        self.assertIn("•  •  •", preview)
        self.assertNotIn("按 Enter 实时打开详情页", preview)

    def test_tui_help_advertises_enter_for_job_detail(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("Enter 详情", text)

    def test_jd_sections_are_rendered_as_scannable_bullets(self):
        module = load_module()
        raw = "\n".join([
            "职位描述",
            "岗位职责：",
            "1、负责 AI Agent 平台建设",
            "2、维护工具链",
            "任职要求：",
            "1、本科及以上",
            "2、熟悉 LangGraph",
        ])

        rendered = module.format_jd_sections(raw)

        self.assertIn("岗位职责", rendered)
        self.assertIn("任职要求", rendered)
        self.assertIn("•", rendered)
        self.assertIn("负责 AI Agent 平台建设", rendered)
        self.assertIn("熟悉 LangGraph", rendered)
        self.assertNotIn("还有", rendered)

    def test_default_jd_body_does_not_show_duplicate_summary_heading(self):
        module = load_module()

        rendered = module.format_jd_sections("职位描述\nGolang\nJava\nPython")

        self.assertIn("•[/] Golang", rendered)
        self.assertIn("•[/] Java", rendered)
        self.assertNotIn("JD 摘要", rendered)

    def test_jd_number_split_does_not_break_dates(self):
        module = load_module()
        raw = "\n".join([
            "职位描述",
            "Golang",
            "毕业时间：2026年 招聘截止日期：2026.07.31",
            "1. 岗位职责",
            "2、任职要求",
        ])

        rendered = module.format_jd_sections(raw)

        self.assertIn("毕业时间：2026年 招聘截止日期：2026.07.31", rendered)
        self.assertIn("•[/] 1. 岗位职责", rendered)
        self.assertIn("•[/] 2、任职要求", rendered)
        self.assertNotIn("•[/] 2\n", rendered)
        self.assertNotIn("•[/] 0\n", rendered)
        self.assertNotIn("•[/] 7.31", rendered)

    def test_project_declares_textual_and_boss_live_entrypoint(self):
        data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))

        self.assertIn("boss-live", data["project"]["scripts"])
        self.assertEqual(data["project"]["scripts"]["boss-live"], "scripts.boss_live_tui:main")
        self.assertEqual(data["project"]["scripts"]["boss-message-probe"], "scripts.boss_message_probe:main")
        self.assertTrue(any(dep.startswith("textual>=") for dep in data["project"]["dependencies"]))


if __name__ == "__main__":
    unittest.main()

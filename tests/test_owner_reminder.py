"""主人的叮嘱插件单元测试。

用标准库 unittest，不依赖 pytest。插件模块按 Runner 的方式（importlib + 子模块搜索路径）
加载，这样 plugin.py 里的相对导入才可用。

注意：这里不导入宿主 ``src.*``（那会连带启动整个 MaiBot），所以注入项与宿主反序列化器的
契约只能靠「结构断言 + 真机验证」保证，不在单测里跑真实宿主。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import IsolatedAsyncioTestCase, TestCase

import importlib.util
import json
import re
import sys
import tempfile
import time

from maibot_sdk import PluginContext, PluginPaths
from maibot_sdk.types import ComponentType

PLUGIN_DIR = Path(__file__).resolve().parents[1]
_MANIFEST = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
# 模块名与插件 id 都不写死：一个取目录名，一个取 manifest
MODULE_NAME = PLUGIN_DIR.name
PLUGIN_ID = _MANIFEST["id"]
MANIFEST_VERSION = _MANIFEST["version"]

PLATFORM = "qq"
OWNER_ID = "100000001"
OWNER = f"{PLATFORM}:{OWNER_ID}"
NON_OWNER_ID = "99999999"
GROUP_ID = "123456789"
GROUP_NAME = "测试群"
TARGET_SESSION = "1111222233334444aaaabbbbccccdddd"
ISSUER_SESSION = "aaaabbbbccccdddd1111222233334444"
OUTSIDER_SESSION = "9999888877776666aaaabbbbccccdddd"
OTHER_ACCOUNT = "100000002"
DUPLICATE_SESSION = "5555666677778888eeeeffff00001111"

COMMAND_NAME = "owner_reminder"

# bot 昵称由宿主配置提供，插件不写死；测试里给一个与真实实例无关的名字
BOT_NICKNAME = "小助手"


def _load_plugin_module() -> Any:
    """按 Runner 的加载方式导入插件模块。"""

    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        str(PLUGIN_DIR / "plugin.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法为插件创建 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


PLUGIN_MODULE = _load_plugin_module()
ALIAS_MODULE = sys.modules[f"{MODULE_NAME}.alias_store"]
ARGS_MODULE = sys.modules[f"{MODULE_NAME}.reminder_args"]
STORE_MODULE = sys.modules[f"{MODULE_NAME}.reminder_store"]
TEXT_MODULE = sys.modules[f"{MODULE_NAME}.reminder_text"]
TARGET_MODULE = sys.modules[f"{MODULE_NAME}.target_resolver"]


class FakeHost:
    """替身宿主：记录能力调用并按需返回。"""

    def __init__(self) -> None:
        self.group_streams: List[Dict[str, Any]] = [
            {
                "session_id": TARGET_SESSION,
                "group_id": GROUP_ID,
                "group_name": GROUP_NAME,
                "account_id": "10001",
                "platform": PLATFORM,
            }
        ]
        self.private_streams: List[Dict[str, Any]] = [
            {
                "session_id": ISSUER_SESSION,
                "user_id": OWNER_ID,
                "user_nickname": "主人",
                "user_cardname": "",
                "account_id": "10001",
                "platform": PLATFORM,
            },
            {
                # 陌生人的私聊流：用来验证"身份必须由流反查"而不是看调用参数
                "session_id": OUTSIDER_SESSION,
                "user_id": NON_OWNER_ID,
                "user_nickname": "路人",
                "user_cardname": "",
                "account_id": "10001",
                "platform": PLATFORM,
            },
        ]
        self.group_streams_error: Optional[str] = None
        self.recent_messages: Dict[str, List[Dict[str, Any]]] = {}
        self.recent_error: Optional[str] = None
        self.sent: List[Tuple[str, str]] = []
        self.proactive_calls: List[Dict[str, Any]] = []
        # 可选：在 maisaka.proactive.trigger 执行期间触发的副作用，用来模拟目标流抢先回复
        self.on_proactive: Optional[Any] = None
        self.proactive_result: Dict[str, Any] = {
            "success": True,
            "queued": True,
            "task_id": f"proactive:{PLUGIN_ID}:1",
        }
        self.config_calls: List[Dict[str, Any]] = []
        self.config_result: Dict[str, Any] = {"success": True, "value": BOT_NICKNAME}
        self.capability_calls: List[str] = []

    async def rpc(
        self,
        method: str,
        plugin_id: str,
        payload: Dict[str, Any],
        timeout_ms: Optional[int] = None,
    ) -> Any:
        """接受 sdk 的 cap.call，并按能力名分发。"""

        if method != "cap.call":
            raise AssertionError(f"未预期的 host 方法: {method}")
        capability = str(payload.get("capability") or "")
        args = payload.get("args") or {}
        self.capability_calls.append(capability)

        if capability == "chat.get_group_streams":
            if self.group_streams_error is not None:
                return {"success": False, "error": self.group_streams_error}
            return {"success": True, "streams": self.group_streams}
        if capability == "chat.get_private_streams":
            return {"success": True, "streams": self.private_streams}
        if capability == "send.text":
            self.sent.append((str(args.get("stream_id")), str(args.get("text"))))
            return {"success": True, "sent": True, "message_id": "m1"}
        if capability == "maisaka.proactive.trigger":
            self.proactive_calls.append(dict(args))
            if self.on_proactive is not None:
                await self.on_proactive()
            return self.proactive_result
        if capability == "config.get":
            self.config_calls.append(dict(args))
            return self.config_result
        if capability == "message.get_recent":
            if self.recent_error is not None:
                return {"success": False, "error": self.recent_error}
            chat_id = str(args.get("chat_id") or "")
            return {"success": True, "messages": list(self.recent_messages.get(chat_id, []))}
        raise AssertionError(f"未预期的能力调用: {capability}")

    @staticmethod
    def recent_message(user_id: str) -> Dict[str, Any]:
        """构造一条 message.get_recent 会返回的消息（只保留身份校验用到的字段）。"""

        return {"message_info": {"user_info": {"user_id": user_id}}}

    @property
    def last_receipt(self) -> str:
        """最近一条回执文本。"""

        return self.sent[-1][1] if self.sent else ""


class PluginTestCase(IsolatedAsyncioTestCase):
    """带替身宿主与临时数据目录的测试基类。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.host = FakeHost()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def build_plugin(self, **sections: Any) -> Any:
        """构造并加载插件实例。"""

        context = PluginContext(
            plugin_id=PLUGIN_ID,
            rpc_call=self.host.rpc,
            paths=PluginPaths(data_dir=self.tmp_dir, runtime_dir=self.tmp_dir),
        )
        plugin = PLUGIN_MODULE.OwnerReminderPlugin()
        plugin._set_context(context)
        plugin.set_plugin_config(self.build_config(**sections))
        await plugin.on_load()
        return plugin

    @staticmethod
    def build_config(**sections: Any) -> Dict[str, Any]:
        """构造插件配置字典；只覆盖传入的配置节。

        配置模型里的 ``owner.owners`` 默认为空（发布版不该带任何人的账号），
        所以这里显式给一个主人，模拟"已经配置好主人"的实例。
        """

        config: Dict[str, Any] = {
            "plugin": {"enabled": True, "config_version": _MANIFEST["version"]},
            "owner": {"owners": [OWNER]},
        }
        config.update(sections)
        return config

    @staticmethod
    def command_kwargs(
        rest: str,
        *,
        user_id: str = OWNER_ID,
        group_id: str = "",
        stream_id: str = ISSUER_SESSION,
    ) -> Dict[str, Any]:
        """构造命令调用参数，忠实模拟宿主捕获的 ``rest``（带前导空格）。"""

        captured_rest = f" {rest}" if rest else ""
        return {
            "text": f"/叮嘱{captured_rest}",
            "stream_id": stream_id,
            "group_id": group_id,
            "platform": PLATFORM,
            "user_id": user_id,
            "matched_groups": {"rest": captured_rest},
        }

    def store_path(self) -> Path:
        """状态文件路径。"""

        return self.tmp_dir / "reminders.json"

    def read_store(self) -> Dict[str, Any]:
        """读取状态文件原始内容。"""

        return json.loads(self.store_path().read_text(encoding="utf-8"))

    @staticmethod
    def seed_reminder(plugin: Any, *, age_seconds: float, content: str = "别跟那个人继续争了") -> Any:
        """绕过命令，直接在状态里放一条"已经过去若干秒"的叮嘱。"""

        reminder, _ = plugin.store.create(
            target_session_id=TARGET_SESSION,
            target_label=GROUP_NAME,
            content=content,
            issuer=OWNER,
            source_session_id=ISSUER_SESSION,
            ttl_seconds=600.0,
            now=time.time() - age_seconds,
        )
        return reminder

    # ──── 下达 ────

    async def test_removed_owner_session_is_forgotten(self) -> None:
        """白名单删掉某个主人后，他的私聊不该还留着代号注入。"""

        plugin = await self.build_plugin()
        plugin._owner_sessions.add("stale-owner-session")

        empty_owner_config = self.build_config(owner={"owners": []})
        plugin.set_plugin_config(empty_owner_config)
        await plugin.on_config_update(
            scope="self",
            config_data=empty_owner_config,
            version=MANIFEST_VERSION,
        )

        self.assertEqual(plugin._owner_sessions, set())

    async def test_set_command_stores_reminder_and_dispatches_once(self) -> None:
        """下达成功：落盘、回执，并按 ask 模式投递一次提醒。"""

        plugin = await self.build_plugin()
        content = "别跟那个人继续争了，语气放软一点"

        success, _, intercept = await plugin.handle_owner_reminder(
            **self.command_kwargs(f"{GROUP_ID} {content}")
        )

        self.assertTrue(success)
        self.assertTrue(intercept)
        self.assertIn("已记下叮嘱", self.host.last_receipt)
        self.assertIn(GROUP_NAME, self.host.last_receipt)

        payload = self.read_store()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(len(payload["reminders"]), 1)
        saved = payload["reminders"][0]
        self.assertEqual(saved["target_session_id"], TARGET_SESSION)
        self.assertEqual(saved["target_label"], GROUP_NAME)
        self.assertEqual(saved["content"], content)
        self.assertEqual(saved["issuer"], OWNER)
        self.assertEqual(saved["source_session_id"], ISSUER_SESSION)
        self.assertEqual(saved["ttl_seconds"], 600.0)

        self.assertEqual(len(self.host.proactive_calls), 1)
        self.assertEqual(self.host.proactive_calls[0]["stream_id"], TARGET_SESSION)
        intent = self.host.proactive_calls[0]["intent"]
        self.assertIn(content, intent)
        self.assertIn("自行判断", intent)

    async def test_silent_mode_never_disturbs_target(self) -> None:
        """silent 模式不调用 proactive.trigger，但叮嘱照样保存。"""

        plugin = await self.build_plugin(dispatch={"mode": "silent"})

        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 早点睡"))

        self.assertEqual(self.host.proactive_calls, [])
        self.assertNotIn("maisaka.proactive.trigger", self.host.capability_calls)
        self.assertEqual(len(self.read_store()["reminders"]), 1)
        self.assertIn("没有被惊动", self.host.last_receipt)

    async def test_dispatch_failure_is_reported_and_reminder_kept(self) -> None:
        """投递失败要在回执里说出来，且不影响叮嘱本身生效。"""

        self.host.proactive_result = {"success": False, "error": "未找到已存在的聊天流: xxx"}
        plugin = await self.build_plugin()

        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别吵了"))

        self.assertIn("提醒没投出去", self.host.last_receipt)
        self.assertIn("未找到已存在的聊天流", self.host.last_receipt)
        self.assertEqual(len(self.read_store()["reminders"]), 1)

    async def test_dispatch_exception_is_reported_and_reminder_kept(self) -> None:
        """投递抛异常（例如 intent 模板写坏）：回执要说明，叮嘱仍然保存。"""

        plugin = await self.build_plugin(dispatch={"intent_template": "主人交代过：{未知占位符}"})

        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别吵了"))

        self.assertIn("提醒投递出错", self.host.last_receipt)
        self.assertEqual(len(self.read_store()["reminders"]), 1)

    async def test_invalid_dispatch_mode_is_rejected(self) -> None:
        """配置写错模式时直接报错，不猜也不静默降级。"""

        plugin = await self.build_plugin(dispatch={"mode": "always"})

        success, _, _ = await plugin.handle_owner_reminder(
            **self.command_kwargs(f"{GROUP_ID} 别吵了")
        )

        self.assertFalse(success)
        self.assertIn("只支持 ask / silent", self.host.last_receipt)
        self.assertFalse(self.store_path().exists())

    # ──── 兑现即消费 ────

    async def test_reply_racing_the_trigger_is_still_consumed(self) -> None:
        """目标流在 proactive.trigger 返回之前就回复，也要算「兑现」。

        trigger 只是入队并唤醒目标流，那一轮的 planner→reply 完全可能先跑完。如果记账发生在
        await 之后，consume_dispatched 会认为"没投递过"而跳过，这条叮嘱就一直留到 TTL，
        期间每轮 planner 继续被注入「主人刚特意交代过」。
        """

        plugin = await self.build_plugin()

        async def reply_during_trigger() -> None:
            await plugin.consume_after_reply(session_id=TARGET_SESSION)

        self.host.on_proactive = reply_during_trigger

        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别吵了"))

        self.assertEqual(plugin.store.active_for(TARGET_SESSION, time.time()), [])

    async def test_dispatch_marks_reminder_dispatched(self) -> None:
        """投递成功要在状态里记下"投递过"，这是之后能被消费的前提。"""

        plugin = await self.build_plugin()

        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别吵了"))

        saved = self.read_store()["reminders"][0]
        self.assertGreater(saved["dispatched_at"], 0.0)

    async def test_reply_consumes_dispatched_reminder(self) -> None:
        """她在目标流开口后，投递过的叮嘱立刻消失。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别吵了"))
        self.assertEqual(len(plugin.store.active_for(TARGET_SESSION, time.time())), 1)

        await plugin.consume_after_reply(session_id=TARGET_SESSION)

        self.assertEqual(plugin.store.active_all(time.time()), [])
        self.assertEqual(self.read_store()["reminders"], [])

    async def test_reply_in_other_stream_does_not_consume(self) -> None:
        """别的聊天流说话不算兑现。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别吵了"))

        await plugin.consume_after_reply(session_id=ISSUER_SESSION)

        self.assertEqual(len(plugin.store.active_for(TARGET_SESSION, time.time())), 1)

    async def test_silent_reminder_is_not_consumed(self) -> None:
        """silent 模式没有投递，她开口也不该把它吃掉，仍按 TTL 兜底。"""

        plugin = await self.build_plugin(dispatch={"mode": "silent"})
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 早点睡"))

        await plugin.consume_after_reply(session_id=TARGET_SESSION)

        self.assertEqual(len(plugin.store.active_for(TARGET_SESSION, time.time())), 1)

    async def test_failed_dispatch_is_not_consumed(self) -> None:
        """投递失败没记 dispatched，因此不会被一次回复顺走。"""

        self.host.proactive_result = {"success": False, "error": "未找到已存在的聊天流: xxx"}
        plugin = await self.build_plugin()
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别吵了"))

        await plugin.consume_after_reply(session_id=TARGET_SESSION)

        self.assertEqual(len(plugin.store.active_for(TARGET_SESSION, time.time())), 1)

    async def test_consume_switch_off_keeps_reminder(self) -> None:
        """关掉 consume_on_reply 就回到纯 TTL 行为。"""

        plugin = await self.build_plugin(reminder={"consume_on_reply": False})
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别吵了"))

        await plugin.consume_after_reply(session_id=TARGET_SESSION)

        self.assertEqual(len(plugin.store.active_for(TARGET_SESSION, time.time())), 1)

    async def test_owner_whitelist_silently_ignores_others(self) -> None:
        """非主人下达：不回执、不落盘。

        注意这里必须用**别人的**私聊流：身份是按 stream_id 反查出来的，
        在主人的私聊流里说话的人就是主人本身。
        """

        plugin = await self.build_plugin()

        success, _, intercept = await plugin.handle_owner_reminder(
            **self.command_kwargs(
                f"{GROUP_ID} 别吵了",
                user_id=NON_OWNER_ID,
                stream_id=OUTSIDER_SESSION,
            )
        )

        self.assertFalse(success)
        self.assertTrue(intercept)
        self.assertEqual(self.host.sent, [])
        self.assertFalse(self.store_path().exists())

    async def test_content_is_truncated_with_notice(self) -> None:
        """内容超上限时截断并提示。"""

        plugin = await self.build_plugin(reminder={"max_content_chars": 5})

        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 一二三四五六七八九十"))

        self.assertEqual(self.read_store()["reminders"][0]["content"], "一二三四五")
        self.assertIn("已截断保存", self.host.last_receipt)

    async def test_missing_content_shows_usage(self) -> None:
        """只有命令词时给用法，不落盘。"""

        plugin = await self.build_plugin()

        success, _, _ = await plugin.handle_owner_reminder(**self.command_kwargs(""))

        self.assertFalse(success)
        self.assertIn("用法", self.host.last_receipt)
        self.assertFalse(self.store_path().exists())

    async def test_target_defaults_to_current_private_stream(self) -> None:
        """省略群号时作用于当前聊天流，并用可读名称回执。"""

        plugin = await self.build_plugin()

        await plugin.handle_owner_reminder(**self.command_kwargs("早点睡"))

        saved = self.read_store()["reminders"][0]
        self.assertEqual(saved["target_session_id"], ISSUER_SESSION)
        self.assertEqual(saved["target_label"], "主人的私聊")

    async def test_current_group_is_resolved_by_group_id(self) -> None:
        """在群里下达且省略群号时，用当前群号反查真实聊天流。"""

        plugin = await self.build_plugin()

        await plugin.handle_owner_reminder(**self.command_kwargs("别吵了", group_id=GROUP_ID))

        saved = self.read_store()["reminders"][0]
        self.assertEqual(saved["target_session_id"], TARGET_SESSION)
        self.assertEqual(saved["target_label"], GROUP_NAME)

    async def test_here_marker_targets_current_stream(self) -> None:
        """``@here`` 作用于当前聊天流，内容不含标记本身。"""

        plugin = await self.build_plugin()

        await plugin.handle_owner_reminder(**self.command_kwargs("@here 早点睡"))

        saved = self.read_store()["reminders"][0]
        self.assertEqual(saved["target_session_id"], ISSUER_SESSION)
        self.assertEqual(saved["content"], "早点睡")

    # ──── 目标解析 ────

    async def test_unknown_group_reports_error_without_writing(self) -> None:
        """群号解析不到：报错、不写状态。"""

        self.host.group_streams = []
        plugin = await self.build_plugin()

        success, _, _ = await plugin.handle_owner_reminder(
            **self.command_kwargs(f"{GROUP_ID} 别吵了")
        )

        self.assertFalse(success)
        self.assertIn("没有找到群", self.host.last_receipt)
        self.assertFalse(self.store_path().exists())

    async def test_group_streams_failure_is_surfaced(self) -> None:
        """能力调用失败：把宿主原始错误带出来。"""

        self.host.group_streams_error = "chat_manager 未就绪"
        plugin = await self.build_plugin()

        success, _, _ = await plugin.handle_owner_reminder(
            **self.command_kwargs(f"{GROUP_ID} 别吵了")
        )

        self.assertFalse(success)
        self.assertIn("chat_manager 未就绪", self.host.last_receipt)

    async def test_ambiguous_group_requires_account_id(self) -> None:
        """同一群号命中多条：先报错要求消歧，指定账号后才放行。"""

        self.host.group_streams = [
            {
                "session_id": TARGET_SESSION,
                "group_id": GROUP_ID,
                "group_name": GROUP_NAME,
                "account_id": "10001",
                "platform": PLATFORM,
            },
            {
                "session_id": DUPLICATE_SESSION,
                "group_id": GROUP_ID,
                "group_name": GROUP_NAME,
                "account_id": OTHER_ACCOUNT,
                "platform": PLATFORM,
            },
        ]

        ambiguous_plugin = await self.build_plugin()
        success, _, _ = await ambiguous_plugin.handle_owner_reminder(
            **self.command_kwargs(f"{GROUP_ID} 别吵了")
        )

        self.assertFalse(success)
        self.assertIn("命中 2 条聊天流", self.host.last_receipt)
        self.assertIn(OTHER_ACCOUNT, self.host.last_receipt)
        self.assertFalse(self.store_path().exists())

        self.host.sent.clear()
        resolved_plugin = await self.build_plugin(target={"account_id": "10001"})
        success, _, _ = await resolved_plugin.handle_owner_reminder(
            **self.command_kwargs(f"{GROUP_ID} 别吵了")
        )

        self.assertTrue(success)
        self.assertEqual(self.read_store()["reminders"][0]["target_session_id"], TARGET_SESSION)

    async def test_configured_account_must_match_even_when_unique(self) -> None:
        """配置了账号却不匹配：即使只命中一条也报错，避免打到旧账号的聊天流。"""

        plugin = await self.build_plugin(target={"account_id": OTHER_ACCOUNT})

        success, _, _ = await plugin.handle_owner_reminder(
            **self.command_kwargs(f"{GROUP_ID} 别吵了")
        )

        self.assertFalse(success)
        self.assertIn("没有聊天流", self.host.last_receipt)
        self.assertIn("10001", self.host.last_receipt)
        self.assertFalse(self.store_path().exists())

    async def test_resolver_rejects_bad_payload(self) -> None:
        """能力返回不是列表时明确报错。"""

        context = PluginContext(
            plugin_id=PLUGIN_ID,
            rpc_call=self.host.rpc,
            paths=PluginPaths(data_dir=self.tmp_dir, runtime_dir=self.tmp_dir),
        )
        self.host.group_streams = ["这不是对象"]

        with self.assertRaises(TARGET_MODULE.TargetResolveError):
            await TARGET_MODULE.resolve_group_target(context.chat, platform=PLATFORM, group_id=GROUP_ID)

    # ──── 注入 ────

    @staticmethod
    def sample_items() -> List[Dict[str, Any]]:
        """构造一份模拟的 planner items。"""

        return [
            {
                "item_type": "UserMessageItem",
                "meta": {
                    "item_id": "u1",
                    "logical_turn_id": None,
                    "timestamp": "2026-10-16T03:00:00",
                },
                "parts": [{"type": "text", "text": "在吗"}],
            }
        ]

    async def test_no_reminder_leaves_items_untouched(self) -> None:
        """没有生效叮嘱时，items 完全不变（宿主据此保持 prompt 原样）。"""

        plugin = await self.build_plugin()
        items = self.sample_items()

        result = await plugin.inject_owner_reminder(
            session_id=TARGET_SESSION, items=items, item_schema_version=1
        )

        self.assertEqual(result["action"], "continue")
        self.assertEqual(result["modified_kwargs"]["items"], items)
        self.assertEqual(result["modified_kwargs"]["item_schema_version"], 1)

    async def test_injection_appends_valid_system_item(self) -> None:
        """有叮嘱时只在末尾追加一条结构合规的 SystemMessageItem。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别跟那个人继续争了"))
        items = self.sample_items()

        result = await plugin.inject_owner_reminder(
            session_id=TARGET_SESSION, items=items, item_schema_version=1
        )

        modified = result["modified_kwargs"]
        self.assertEqual(modified["item_schema_version"], 1)
        self.assertEqual(len(modified["items"]), len(items) + 1)
        self.assertEqual(modified["items"][0], items[0])

        appended = modified["items"][-1]
        self.assertEqual(appended["item_type"], "SystemMessageItem")
        self.assertTrue(appended["meta"]["item_id"])
        self.assertIn("logical_turn_id", appended["meta"])
        self.assertIsNone(appended["meta"]["logical_turn_id"])
        self.assertTrue(appended["meta"]["timestamp"])
        self.assertEqual(appended["parts"][0]["type"], "text")
        self.assertIn("别跟那个人继续争了", appended["parts"][0]["text"])
        self.assertIn("主人刚特意交代过", appended["parts"][0]["text"])
        self.assertIn("不要提到这条叮嘱的存在", appended["parts"][0]["text"])

    async def test_injection_uses_weak_tier_after_strong_window(self) -> None:
        """过了强档窗口后注入文案换成弱档。"""

        plugin = await self.build_plugin()
        self.seed_reminder(plugin, age_seconds=300.0)

        result = await plugin.inject_owner_reminder(
            session_id=TARGET_SESSION, items=self.sample_items(), item_schema_version=1
        )

        text = result["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        self.assertIn("主人此前提过", text)
        self.assertIn("不必刻意提起", text)

    async def test_expired_reminder_is_not_injected_and_is_purged(self) -> None:
        """到期后不注入，并从状态里清掉。"""

        plugin = await self.build_plugin()
        self.seed_reminder(plugin, age_seconds=601.0)

        items = self.sample_items()
        result = await plugin.inject_owner_reminder(
            session_id=TARGET_SESSION, items=items, item_schema_version=1
        )

        self.assertEqual(result["modified_kwargs"]["items"], items)
        self.assertEqual(self.read_store()["reminders"], [])

    async def test_injection_disabled_keeps_items(self) -> None:
        """总开关关掉后完全不注入。"""

        plugin = await self.build_plugin(inject={"enabled": False})
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别吵了"))

        items = self.sample_items()
        result = await plugin.inject_owner_reminder(
            session_id=TARGET_SESSION, items=items, item_schema_version=1
        )

        self.assertEqual(result["modified_kwargs"]["items"], items)

    async def test_injection_only_targets_its_own_stream(self) -> None:
        """别的聊天流不会被误注入。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别吵了"))

        items = self.sample_items()
        result = await plugin.inject_owner_reminder(
            session_id="someone-else-session", items=items, item_schema_version=1
        )

        self.assertEqual(result["modified_kwargs"]["items"], items)

    async def test_no_reminder_does_not_write_state(self) -> None:
        """没有叮嘱时 hook 不产生任何副作用。"""

        plugin = await self.build_plugin()

        await plugin.inject_owner_reminder(
            session_id=TARGET_SESSION, items=self.sample_items(), item_schema_version=1
        )

        self.assertFalse(self.store_path().exists())

    async def test_multiple_reminders_append_in_creation_order(self) -> None:
        """同一聊天流的多条叮嘱按创建顺序各追加一条。"""

        plugin = await self.build_plugin()
        self.seed_reminder(plugin, age_seconds=30.0, content="第一条")
        self.seed_reminder(plugin, age_seconds=10.0, content="第二条")

        result = await plugin.inject_owner_reminder(
            session_id=TARGET_SESSION, items=self.sample_items(), item_schema_version=1
        )

        appended = result["modified_kwargs"]["items"][1:]
        self.assertEqual(len(appended), 2)
        self.assertIn("第一条", appended[0]["parts"][0]["text"])
        self.assertIn("第二条", appended[1]["parts"][0]["text"])
        self.assertNotEqual(appended[0]["meta"]["item_id"], appended[1]["meta"]["item_id"])

    # ──── 列表与撤销 ────

    async def test_list_shows_stage_and_remaining(self) -> None:
        """列表显示档位、剩余时间与内容。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别跟那个人继续争了"))
        self.host.sent.clear()

        await plugin.handle_owner_reminder(**self.command_kwargs("列表"))

        receipt = self.host.last_receipt
        self.assertIn("生效中的叮嘱（1 条）", receipt)
        self.assertIn("【强】", receipt)
        self.assertIn("别跟那个人继续争了", receipt)
        self.assertIn("剩余 09:", receipt)

    async def test_list_accepts_english_alias(self) -> None:
        """``/influence list`` 也能查看列表。"""

        plugin = await self.build_plugin()
        self.seed_reminder(plugin, age_seconds=1.0)

        await plugin.handle_owner_reminder(**self.command_kwargs("list"))

        self.assertIn("生效中的叮嘱（1 条）", self.host.last_receipt)

    async def test_list_marks_weak_tier(self) -> None:
        """弱档条目在列表里标成【弱】。"""

        plugin = await self.build_plugin()
        self.seed_reminder(plugin, age_seconds=400.0)

        await plugin.handle_owner_reminder(**self.command_kwargs("列表"))

        self.assertIn("【弱】", self.host.last_receipt)

    async def test_list_without_reminders(self) -> None:
        """空列表给明确提示。"""

        plugin = await self.build_plugin()

        await plugin.handle_owner_reminder(**self.command_kwargs("列表"))

        self.assertIn("当前没有生效中的叮嘱", self.host.last_receipt)

    async def test_revoke_single_and_all(self) -> None:
        """按编号撤销与全部撤销。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 第一条"))
        first_id = self.read_store()["reminders"][0]["id"]
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 第二条"))
        self.host.sent.clear()

        success, _, _ = await plugin.handle_owner_reminder(
            **self.command_kwargs(f"撤销 {first_id}")
        )
        self.assertTrue(success)
        self.assertIn(f"已撤销叮嘱 #{first_id}", self.host.last_receipt)
        self.assertEqual(len(self.read_store()["reminders"]), 1)

        await plugin.handle_owner_reminder(**self.command_kwargs("撤销 all"))
        self.assertIn("已撤销全部 1 条叮嘱", self.host.last_receipt)
        self.assertEqual(self.read_store()["reminders"], [])

    async def test_revoke_unknown_id_reports_missing(self) -> None:
        """撤销不存在的编号：明确提示，不改状态。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别吵了"))
        self.host.sent.clear()

        success, _, _ = await plugin.handle_owner_reminder(**self.command_kwargs("撤销 zzzz"))

        self.assertFalse(success)
        self.assertIn("没有找到编号为 zzzz", self.host.last_receipt)
        self.assertEqual(len(self.read_store()["reminders"]), 1)

    async def test_revoke_without_argument_shows_usage(self) -> None:
        """撤销不带参数时给用法。"""

        plugin = await self.build_plugin()

        success, _, _ = await plugin.handle_owner_reminder(**self.command_kwargs("撤销"))

        self.assertFalse(success)
        self.assertIn("用法", self.host.last_receipt)

    async def test_revoke_rejects_extra_arguments(self) -> None:
        """撤销参数多于一个时给用法，不瞎猜。"""

        plugin = await self.build_plugin()

        success, _, _ = await plugin.handle_owner_reminder(**self.command_kwargs("撤销 a1b2 c3d4"))

        self.assertFalse(success)
        self.assertIn("用法", self.host.last_receipt)

    async def test_max_per_target_replaces_oldest(self) -> None:
        """同一聊天流超过上限时替换最旧的一条。"""

        plugin = await self.build_plugin(reminder={"max_per_target": 2})

        for content in ("第一条", "第二条", "第三条"):
            await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} {content}"))

        payload = self.read_store()
        contents = [item["content"] for item in payload["reminders"]]
        self.assertEqual(contents, ["第二条", "第三条"])
        self.assertIn("已替换掉最旧的", self.host.last_receipt)


class CommandComponentTestCase(TestCase):
    """命令组件的声明与匹配。"""

    @classmethod
    def setUpClass(cls) -> None:
        plugin = PLUGIN_MODULE.OwnerReminderPlugin()
        cls.components = {
            component["name"]: component
            for component in plugin.get_components()
            if "command_pattern" in component["metadata"]
        }

    def test_single_command_component(self) -> None:
        """只有一个命令组件，子命令在插件内部分派。"""

        self.assertEqual(sorted(self.components), [COMMAND_NAME])
        self.assertIn("下达", self.components[COMMAND_NAME]["metadata"]["description"])

    def test_pattern_captures_rest(self) -> None:
        """正则把命令词之后的内容原样交给插件解析。"""

        pattern = re.compile(self.components[COMMAND_NAME]["metadata"]["command_pattern"])
        self.assertEqual(pattern.search("/叮嘱").groupdict()["rest"], "")
        self.assertEqual(
            pattern.search("/叮嘱 123456789 别吵了").groupdict()["rest"], " 123456789 别吵了"
        )
        self.assertEqual(pattern.search("/influence list").groupdict()["rest"], " list")
        self.assertIsNone(pattern.search("前缀 /叮嘱 别吵了"))
        # 别把 /influencer 这种同前缀的普通消息吞成命令
        self.assertIsNone(pattern.search("/influencer 你好"))
        self.assertIsNone(pattern.search("/influence2 你好"))

    def test_hook_is_declared(self) -> None:
        """planner 注入 hook 是 blocking + late；消费 hook 也是 blocking。"""

        hook_metadata = {
            component["name"]: component["metadata"]
            for component in PLUGIN_MODULE.OwnerReminderPlugin().get_components()
            if component["type"] == ComponentType.HOOK_HANDLER.value
        }
        self.assertIn("owner_reminder_inject", hook_metadata)
        inject_metadata = hook_metadata["owner_reminder_inject"]
        self.assertEqual(inject_metadata["hook"], "maisaka.planner.before_request")
        self.assertEqual(inject_metadata["mode"], "blocking")
        self.assertEqual(inject_metadata["order"], "late")

        self.assertIn("owner_reminder_consume", hook_metadata)
        consume_metadata = hook_metadata["owner_reminder_consume"]
        self.assertEqual(consume_metadata["hook"], "maisaka.reply.before_post_process")
        self.assertEqual(consume_metadata["mode"], "blocking")

    def test_tools_are_declared_private_scoped(self) -> None:
        """两个自然语言工具都必须把 chat_scope=private 暴露在**顶层**。

        Runner 读的是顶层 `chat_scope`（`runner_main.py:1420`），不是 metadata 里的，
        所以这里按 Runner 的读法断言，避免"写在 metadata 里看起来对、实际不生效"。
        """

        components = {
            component["name"]: component
            for component in PLUGIN_MODULE.OwnerReminderPlugin().get_components()
            if component["type"] == ComponentType.TOOL.value
        }

        self.assertEqual(sorted(components), ["owner_chat_alias", "owner_directive"])
        for component in components.values():
            self.assertEqual(component.get("chat_scope", "all"), "private")
        directive_description = components["owner_directive"]["metadata"]["description"]
        self.assertIn("主人", directive_description)
        self.assertIn("不要搬运私聊原文", directive_description)
        self.assertTrue(components["owner_directive"]["metadata"]["parameters"])
        self.assertTrue(components["owner_chat_alias"]["metadata"]["parameters"])


class OwnerEntryAndDurationTestCase(TestCase):
    """主人条目的格式校验与时长文案。"""

    def test_owner_entry_requires_ascii_colon(self) -> None:
        """全角冒号这类写法永远匹配不上白名单，必须能被识别成「格式不对」。"""

        self.assertIsNone(PLUGIN_MODULE._split_owner("qq：123456789"))
        self.assertIsNone(PLUGIN_MODULE._split_owner("qq123456789"))
        self.assertIsNone(PLUGIN_MODULE._split_owner(":123"))
        self.assertIsNone(PLUGIN_MODULE._split_owner("qq:"))
        self.assertEqual(PLUGIN_MODULE._split_owner(" qq : 123 "), ("qq", "123"))

    def test_duration_keeps_half_minutes(self) -> None:
        self.assertEqual(TEXT_MODULE.format_duration(90), "1 分 30 秒")
        self.assertEqual(TEXT_MODULE.format_duration(120), "2 分钟")
        self.assertEqual(TEXT_MODULE.format_duration(45), "45 秒")


class ArgumentParseTestCase(TestCase):
    """命令参数解析。"""

    def test_set_with_group_id(self) -> None:
        parsed = ARGS_MODULE.parse_command("123456789 别吵了")
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.action, ARGS_MODULE.ACTION_SET)
        self.assertEqual(parsed.target_group_id, "123456789")
        self.assertEqual(parsed.content, "别吵了")

    def test_set_with_here(self) -> None:
        parsed = ARGS_MODULE.parse_command("@here 早点睡")
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.target_group_id, "")
        self.assertEqual(parsed.content, "早点睡")

    def test_set_with_plain_content(self) -> None:
        parsed = ARGS_MODULE.parse_command("别跟那个人吵了")
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.target_group_id, "")
        self.assertEqual(parsed.content, "别跟那个人吵了")

    def test_set_keeps_multiline_content(self) -> None:
        parsed = ARGS_MODULE.parse_command("123456789 第一行\n第二行")
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.content, "第一行\n第二行")

    def test_content_may_look_like_subcommand(self) -> None:
        """内容里出现"列表"字样不会被误判成子命令。"""

        parsed = ARGS_MODULE.parse_command("列表里的那个人别吵了")
        self.assertEqual(parsed.action, ARGS_MODULE.ACTION_SET)
        self.assertEqual(parsed.content, "列表里的那个人别吵了")

    def test_list_words(self) -> None:
        for text in ("列表", "list"):
            parsed = ARGS_MODULE.parse_command(text)
            self.assertEqual(parsed.action, ARGS_MODULE.ACTION_LIST)
            self.assertTrue(parsed.ok)

    def test_list_rejects_extra_arguments(self) -> None:
        parsed = ARGS_MODULE.parse_command("列表 多余")
        self.assertEqual(parsed.action, ARGS_MODULE.ACTION_LIST)
        self.assertFalse(parsed.ok)

    def test_revoke_words(self) -> None:
        for text in ("撤销 a1b2", "revoke a1b2"):
            parsed = ARGS_MODULE.parse_command(text)
            self.assertEqual(parsed.action, ARGS_MODULE.ACTION_REVOKE)
            self.assertEqual(parsed.argument, "a1b2")

    def test_revoke_requires_single_argument(self) -> None:
        self.assertFalse(ARGS_MODULE.parse_command("撤销").ok)
        self.assertFalse(ARGS_MODULE.parse_command("撤销 a1b2 c3d4").ok)

    def test_error_cases(self) -> None:
        self.assertFalse(ARGS_MODULE.parse_command("").ok)
        self.assertFalse(ARGS_MODULE.parse_command("@here").ok)
        self.assertFalse(ARGS_MODULE.parse_command("123456789").ok)

    def test_revoke_all_markers(self) -> None:
        self.assertTrue(ARGS_MODULE.is_revoke_all("all"))
        self.assertTrue(ARGS_MODULE.is_revoke_all("全部"))
        self.assertFalse(ARGS_MODULE.is_revoke_all("a1b2"))

    def test_alias_aware_target_parsing(self) -> None:
        """传入 is_alias 后，首个 token 是代号时也当成目标。"""

        known = {"技术群"}
        parsed = ARGS_MODULE.parse_command("技术群 别吵了", is_alias=known.__contains__)
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.target_group_id, "技术群")
        self.assertEqual(parsed.content, "别吵了")

        missing_content = ARGS_MODULE.parse_command("技术群", is_alias=known.__contains__)
        self.assertFalse(missing_content.ok)
        self.assertIn("后面缺少叮嘱内容", missing_content.error)

        # 不认识的 token 仍然按"作用于当前聊天流"处理
        plain = ARGS_MODULE.parse_command("别跟那个人吵了", is_alias=known.__contains__)
        self.assertEqual(plain.target_group_id, "")
        self.assertEqual(plain.content, "别跟那个人吵了")

        # 子命令优先级高于代号
        listed = ARGS_MODULE.parse_command("列表", is_alias=known.__contains__)
        self.assertEqual(listed.action, ARGS_MODULE.ACTION_LIST)


class StoreTestCase(TestCase):
    """状态存储与时效衰减（不经过插件上下文）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = STORE_MODULE.ReminderStore(
            Path(self._tmp.name), max_per_target=3, strong_ratio=0.33
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create(self, *, now: float, content: str = "别吵了", ttl: float = 600.0) -> Any:
        reminder, _ = self.store.create(
            target_session_id=TARGET_SESSION,
            target_label=GROUP_NAME,
            content=content,
            issuer=OWNER,
            source_session_id=ISSUER_SESSION,
            ttl_seconds=ttl,
            now=now,
        )
        return reminder

    def test_stage_boundaries(self) -> None:
        """强档 / 弱档 / 到期的边界。"""

        created = 1000.0
        reminder = self._create(now=created)
        self.assertEqual(reminder.stage(created, 0.33), "strong")
        self.assertEqual(reminder.stage(created + 197.9, 0.33), "strong")
        self.assertEqual(reminder.stage(created + 198.0, 0.33), "weak")
        self.assertEqual(reminder.stage(created + 599.9, 0.33), "weak")
        self.assertEqual(reminder.stage(created + 600.0, 0.33), "expired")

    def test_write_failure_keeps_memory_unchanged(self) -> None:
        """写盘失败时内存必须保持旧值。

        回执说「没保存」就真的要没保存：否则磁盘满 / 权限错误之后，那条叮嘱仍会留在内存里
        继续注入目标流，下一次任意成功写盘还会把它持久化。
        """

        first = self._create(now=1000.0, content="第一条")
        original = self.store._write_file

        def boom(_items: Any) -> None:
            raise STORE_MODULE.ReminderStoreError("磁盘满")

        self.store._write_file = boom  # type: ignore[assignment]
        try:
            with self.assertRaises(STORE_MODULE.ReminderStoreError):
                self._create(now=1001.0, content="第二条")
        finally:
            self.store._write_file = original  # type: ignore[assignment]

        self.assertEqual([item.id for item in self.store.active_all(1002.0)], [first.id])
        reopened = STORE_MODULE.ReminderStore(Path(self._tmp.name), max_per_target=3, strong_ratio=0.33)
        self.assertEqual([item.content for item in reopened.active_all(1002.0)], ["第一条"])

    def test_purge_and_active(self) -> None:
        """到期条目被清除，未到期条目保留。"""

        self._create(now=1000.0)
        self.assertEqual(len(self.store.active_for(TARGET_SESSION, 1100.0)), 1)
        self.assertEqual(self.store.active_for(TARGET_SESSION, 1601.0), [])
        self.assertEqual(self.store.active_all(1601.0), [])

    def test_remaining_seconds_never_negative(self) -> None:
        """剩余时间不会为负。"""

        reminder = self._create(now=1000.0)
        self.assertEqual(reminder.remaining_seconds(1000.0), 600.0)
        self.assertEqual(reminder.remaining_seconds(2000.0), 0.0)

    def test_write_is_atomic_and_leaves_no_temp_file(self) -> None:
        """原子写不会遗留临时文件。"""

        self._create(now=1000.0)
        leftovers = sorted(path.name for path in Path(self._tmp.name).iterdir())
        self.assertEqual(leftovers, ["reminders.json"])

    def test_state_survives_reload(self) -> None:
        """重新打开存储后状态仍在（模拟插件重启）。"""

        reminder = self._create(now=1000.0)
        reopened = STORE_MODULE.ReminderStore(
            Path(self._tmp.name), max_per_target=3, strong_ratio=0.33
        )
        active = reopened.active_all(1001.0)
        self.assertEqual([item.id for item in active], [reminder.id])

    def test_corrupt_store_raises(self) -> None:
        """状态文件损坏时直接抛错，不静默清空。"""

        self.store.path.write_text("{ 这不是 JSON", encoding="utf-8")
        with self.assertRaises(STORE_MODULE.ReminderStoreError):
            self.store.active_all(1000.0)

    def test_unsupported_version_raises(self) -> None:
        """版本不匹配时抛错。"""

        self.store.path.write_text(json.dumps({"version": 99, "reminders": []}), encoding="utf-8")
        with self.assertRaises(STORE_MODULE.ReminderStoreError):
            self.store.active_all(1000.0)

    def test_missing_field_raises(self) -> None:
        """条目缺字段时抛错，不猜默认值。"""

        self.store.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "reminders": [
                        {
                            "id": "a1b2",
                            "target_session_id": TARGET_SESSION,
                            "content": "别吵",
                            "issuer": OWNER,
                            "source_session_id": ISSUER_SESSION,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(STORE_MODULE.ReminderStoreError):
            self.store.active_all(1000.0)

    def test_revoke_all_returns_removed(self) -> None:
        """全部撤销返回被删除的条目。"""

        self._create(now=1000.0, content="第一条")
        self._create(now=1001.0, content="第二条")
        removed = self.store.revoke_all(1002.0)
        self.assertEqual(len(removed), 2)
        self.assertEqual(self.store.active_all(1002.0), [])

    def test_mark_dispatched_persists_and_survives_reload(self) -> None:
        """投递时刻落盘，重开存储后仍记得"投递过"。"""

        reminder = self._create(now=1000.0)
        self.assertFalse(reminder.dispatched)

        updated = self.store.mark_dispatched(reminder.id, 1005.0)

        self.assertIsNotNone(updated)
        self.assertEqual(updated.dispatched_at, 1005.0)
        self.assertTrue(updated.dispatched)

        reopened = STORE_MODULE.ReminderStore(
            Path(self._tmp.name), max_per_target=3, strong_ratio=0.33
        )
        self.assertEqual([item.dispatched for item in reopened.active_all(1006.0)], [True])

    def test_mark_dispatched_unknown_id_returns_none(self) -> None:
        """编号不存在时返回 None，不凭空造条目。"""

        self._create(now=1000.0)

        self.assertIsNone(self.store.mark_dispatched("不存在", 1001.0))
        self.assertEqual(len(self.store.active_all(1001.0)), 1)

    def test_consume_only_touches_dispatched_items_of_target(self) -> None:
        """只消费目标流里投递过的条目：没投递的、别的流的都不动。"""

        dispatched = self._create(now=1000.0, content="投递过的")
        self.store.mark_dispatched(dispatched.id, 1001.0)
        silent, _ = self.store.create(
            target_session_id=TARGET_SESSION,
            target_label=GROUP_NAME,
            content="没投递的",
            issuer=OWNER,
            source_session_id=ISSUER_SESSION,
            ttl_seconds=600.0,
            now=1002.0,
        )
        other, _ = self.store.create(
            target_session_id="别的会话",
            target_label="别的群",
            content="别的群的",
            issuer=OWNER,
            source_session_id=ISSUER_SESSION,
            ttl_seconds=600.0,
            now=1003.0,
        )

        consumed = self.store.consume_dispatched(TARGET_SESSION, 1004.0)

        self.assertEqual([item.id for item in consumed], [dispatched.id])
        self.assertEqual(
            {item.id for item in self.store.active_all(1004.0)}, {silent.id, other.id}
        )

    def test_consume_without_hits_leaves_store_untouched(self) -> None:
        """没有可消费条目时不写盘、不误删。"""

        self._create(now=1000.0)

        self.assertEqual(self.store.consume_dispatched(TARGET_SESSION, 1001.0), [])
        self.assertEqual(self.store.consume_dispatched("没有的会话", 1001.0), [])

        self.assertEqual(len(self.store.active_all(1001.0)), 1)
        self.assertEqual(self.read_dispatched_at(), 0.0)

    def read_dispatched_at(self) -> float:
        """读取状态文件里第一条叮嘱的投递时刻。"""

        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        return float(payload["reminders"][0]["dispatched_at"])


class AliasAndToolTestCase(PluginTestCase):
    """二期：自然语言工具与群代号。"""

    @staticmethod
    def tool_kwargs(
        *,
        user_id: str = OWNER_ID,
        stream_id: str = ISSUER_SESSION,
        group_id: str = "",
    ) -> Dict[str, Any]:
        """构造工具调用参数，模拟宿主补齐的会话上下文。"""

        return {
            "stream_id": stream_id,
            "chat_id": stream_id,
            "group_id": group_id,
            "platform": PLATFORM,
            "user_id": user_id,
        }

    def read_aliases(self) -> Dict[str, Any]:
        """读取代号表原始内容。"""

        return json.loads((self.tmp_dir / "aliases.json").read_text(encoding="utf-8"))

    # ──── 代号维护 ────

    async def test_alias_set_list_remove(self) -> None:
        """代号的增、查、删。"""

        plugin = await self.build_plugin()

        saved = await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **self.tool_kwargs()
        )
        self.assertTrue(saved["success"])
        self.assertIn("记住了", saved["content"])
        self.assertIn("技术群", saved["content"])
        self.assertEqual(self.read_aliases()["version"], 1)
        entry = self.read_aliases()["aliases"][0]
        self.assertEqual(entry["name"], "技术群")
        self.assertEqual(entry["group_id"], GROUP_ID)
        self.assertEqual(entry["group_label"], GROUP_NAME)
        self.assertEqual(entry["platform"], PLATFORM)

        listed = await plugin.handle_owner_chat_alias(action="list", **self.tool_kwargs())
        self.assertTrue(listed["success"])
        self.assertIn("群代号（1 个）", listed["content"])
        self.assertIn(f"技术群 = {GROUP_ID}", listed["content"])
        self.assertIn(GROUP_NAME, listed["content"])

        removed = await plugin.handle_owner_chat_alias(
            action="remove", alias="技术群", **self.tool_kwargs()
        )
        self.assertTrue(removed["success"])
        self.assertIn("已删除代号", removed["content"])
        self.assertEqual(self.read_aliases()["aliases"], [])

        again = await plugin.handle_owner_chat_alias(
            action="remove", alias="技术群", **self.tool_kwargs()
        )
        self.assertFalse(again["success"])
        self.assertIn("没有代号", again["content"])

    async def test_alias_list_empty_and_default_action(self) -> None:
        """没有代号时的列表提示；action 空串按列表处理。"""

        plugin = await self.build_plugin()

        result = await plugin.handle_owner_chat_alias(**self.tool_kwargs())

        self.assertTrue(result["success"])
        self.assertIn("还没有设置任何群代号", result["content"])

    async def test_alias_set_rejects_bad_name(self) -> None:
        """纯数字、带空白、过长的代号都要被拒绝。"""

        plugin = await self.build_plugin()

        numeric = await plugin.handle_owner_chat_alias(
            action="set", alias="123456", group=GROUP_ID, **self.tool_kwargs()
        )
        self.assertFalse(numeric["success"])
        self.assertIn("纯数字", numeric["content"])

        spaced = await plugin.handle_owner_chat_alias(
            action="set", alias="技术 群", group=GROUP_ID, **self.tool_kwargs()
        )
        self.assertFalse(spaced["success"])
        self.assertIn("空格", spaced["content"])

        too_long = await plugin.handle_owner_chat_alias(
            action="set", alias="代" * 21, group=GROUP_ID, **self.tool_kwargs()
        )
        self.assertFalse(too_long["success"])
        self.assertIn("不能超过", too_long["content"])

        self.assertFalse((self.tmp_dir / "aliases.json").exists())

    async def test_alias_set_requires_existing_group(self) -> None:
        """群号不存在时不许登记代号。"""

        self.host.group_streams = []
        plugin = await self.build_plugin()

        result = await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **self.tool_kwargs()
        )

        self.assertFalse(result["success"])
        self.assertIn("没有找到群", result["content"])
        self.assertFalse((self.tmp_dir / "aliases.json").exists())

    async def test_alias_set_overwrites_same_name(self) -> None:
        """同名代号是覆盖，不是叠加。"""

        self.host.group_streams = [
            {
                "session_id": TARGET_SESSION,
                "group_id": GROUP_ID,
                "group_name": GROUP_NAME,
                "account_id": "10001",
                "platform": PLATFORM,
            },
            {
                "session_id": DUPLICATE_SESSION,
                "group_id": "987654321",
                "group_name": "另一个群",
                "account_id": "10001",
                "platform": PLATFORM,
            },
        ]
        plugin = await self.build_plugin()

        await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **self.tool_kwargs()
        )
        await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group="987654321", **self.tool_kwargs()
        )

        aliases = self.read_aliases()["aliases"]
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0]["group_id"], "987654321")

    async def test_alias_unknown_action(self) -> None:
        """不认识的 action 要明确报错。"""

        plugin = await self.build_plugin()

        result = await plugin.handle_owner_chat_alias(action="rename", **self.tool_kwargs())

        self.assertFalse(result["success"])
        self.assertIn("只支持 set / remove / list", result["content"])

    async def test_tools_reject_non_owner(self) -> None:
        """非主人调用两个工具：一律拒绝且不落盘。"""

        plugin = await self.build_plugin()
        outsider = self.tool_kwargs(user_id=NON_OWNER_ID, stream_id=OUTSIDER_SESSION)

        directive = await plugin.handle_owner_directive(content="别吵了", chat=GROUP_ID, **outsider)
        alias = await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **outsider
        )

        self.assertFalse(directive["success"])
        self.assertFalse(alias["success"])
        self.assertEqual(directive["content"], "没有权限执行该操作。")
        self.assertEqual(alias["content"], "没有权限执行该操作。")
        self.assertFalse(self.store_path().exists())
        self.assertFalse((self.tmp_dir / "aliases.json").exists())

    async def test_tools_can_be_disabled(self) -> None:
        """tool.enabled=false 时两个工具都拒绝执行。"""

        plugin = await self.build_plugin(tool={"enabled": False})

        directive = await plugin.handle_owner_directive(content="别吵了", **self.tool_kwargs())
        alias = await plugin.handle_owner_chat_alias(action="list", **self.tool_kwargs())

        self.assertFalse(directive["success"])
        self.assertFalse(alias["success"])
        self.assertIn("已关闭", directive["content"])
        self.assertFalse(self.store_path().exists())

    # ──── 自然语言下达 ────

    async def test_directive_tool_with_alias(self) -> None:
        """用代号下达：落到代号对应的真实聊天流，回执里带上代号。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **self.tool_kwargs()
        )

        result = await plugin.handle_owner_directive(
            content="别继续和某人争执，语气放软", chat="技术群", **self.tool_kwargs()
        )

        self.assertTrue(result["success"])
        self.assertIn("《技术群》", result["content"])
        self.assertIn(GROUP_NAME, result["content"])

        payload = self.read_store()
        self.assertEqual(len(payload["reminders"]), 1)
        saved = payload["reminders"][0]
        self.assertEqual(saved["target_session_id"], TARGET_SESSION)
        self.assertEqual(saved["content"], "别继续和某人争执，语气放软")
        self.assertEqual(saved["issuer"], OWNER)
        self.assertEqual(saved["source_session_id"], ISSUER_SESSION)
        self.assertEqual(len(self.host.proactive_calls), 1)
        self.assertIn("别继续和某人争执", self.host.proactive_calls[0]["intent"])

    async def test_directive_tool_accepts_group_id_and_defaults(self) -> None:
        """chat 参数：群号可用；留空则作用于当前聊天流。"""

        plugin = await self.build_plugin()

        by_id = await plugin.handle_owner_directive(
            content="别吵了", chat=GROUP_ID, **self.tool_kwargs()
        )
        self.assertTrue(by_id["success"])
        self.assertEqual(self.read_store()["reminders"][0]["target_session_id"], TARGET_SESSION)

        current = await plugin.handle_owner_directive(content="早点睡", **self.tool_kwargs())
        self.assertTrue(current["success"])
        targets = [item["target_session_id"] for item in self.read_store()["reminders"]]
        self.assertIn(ISSUER_SESSION, targets)

    async def test_directive_tool_unknown_alias_lists_available(self) -> None:
        """代号不认识时，把可用代号回给模型，便于自己纠正。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **self.tool_kwargs()
        )

        result = await plugin.handle_owner_directive(
            content="别吵了", chat="羊群", **self.tool_kwargs()
        )

        self.assertFalse(result["success"])
        self.assertIn("没有找到代号「羊群」", result["content"])
        self.assertIn("技术群", result["content"])
        self.assertFalse(self.store_path().exists())

    async def test_directive_tool_without_any_alias(self) -> None:
        """一条代号都没有时，明确提示直接给群号。"""

        plugin = await self.build_plugin()

        result = await plugin.handle_owner_directive(
            content="别吵了", chat="羊群", **self.tool_kwargs()
        )

        self.assertFalse(result["success"])
        self.assertIn("还没有设置任何群代号", result["content"])

    async def test_directive_tool_requires_content(self) -> None:
        """缺少 content 时明确报错，不落盘。"""

        plugin = await self.build_plugin()

        result = await plugin.handle_owner_directive(chat=GROUP_ID, **self.tool_kwargs())

        self.assertFalse(result["success"])
        self.assertIn("缺少叮嘱内容", result["content"])
        self.assertFalse(self.store_path().exists())

    async def test_tool_and_command_write_same_shape(self) -> None:
        """两条通道落盘结构一致（同一份实现）。"""

        plugin = await self.build_plugin()

        await plugin.handle_owner_directive(
            content="工具下达", chat=GROUP_ID, **self.tool_kwargs()
        )
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 命令下达"))

        reminders = self.read_store()["reminders"]
        self.assertEqual(len(reminders), 2)
        self.assertEqual(set(reminders[0]), set(reminders[1]))
        self.assertEqual(reminders[0]["issuer"], reminders[1]["issuer"])
        self.assertEqual(reminders[0]["source_session_id"], reminders[1]["source_session_id"])
        self.assertEqual(
            [item["content"] for item in reminders], ["工具下达", "命令下达"]
        )

    async def test_command_accepts_alias_too(self) -> None:
        """/叮嘱 也支持用代号指定目标。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **self.tool_kwargs()
        )

        success, _, _ = await plugin.handle_owner_reminder(
            **self.command_kwargs("技术群 别吵了")
        )

        self.assertTrue(success)
        self.assertEqual(self.read_store()["reminders"][0]["target_session_id"], TARGET_SESSION)
        self.assertIn("《技术群》", self.host.last_receipt)

    # ──── 主人私聊里的代号对照注入 ────

    async def test_owner_note_injected_in_owner_private_chat(self) -> None:
        """主人私聊 + 有代号 → 恰好注入一条代号对照。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **self.tool_kwargs()
        )
        items = self.sample_items()

        result = await plugin.inject_owner_reminder(
            session_id=ISSUER_SESSION, items=items, item_schema_version=1
        )

        modified = result["modified_kwargs"]["items"]
        self.assertEqual(len(modified), len(items) + 1)
        note = modified[-1]["parts"][0]["text"]
        self.assertIn("群代号", note)
        self.assertIn("owner_directive", note)
        self.assertIn(f"技术群={GROUP_ID}", note)

    async def test_owner_note_uses_host_bot_nickname(self) -> None:
        """代号对照里的 bot 称呼来自宿主配置，不写死在插件里。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **self.tool_kwargs()
        )

        result = await plugin.inject_owner_reminder(
            session_id=ISSUER_SESSION, items=self.sample_items(), item_schema_version=1
        )

        note = result["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        self.assertIn(BOT_NICKNAME, note)
        self.assertIn("config.get", self.host.capability_calls)
        self.assertEqual(self.host.config_calls[0]["key"], "bot.nickname")

    async def test_owner_note_falls_back_when_nickname_missing(self) -> None:
        """读不到昵称时退回中性的「你」，不报错也不阻碍加载。"""

        self.host.config_result = {"success": False, "value": None, "error": "配置项不存在"}
        plugin = await self.build_plugin()
        await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **self.tool_kwargs()
        )

        result = await plugin.inject_owner_reminder(
            session_id=ISSUER_SESSION, items=self.sample_items(), item_schema_version=1
        )

        note = result["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        self.assertIn("主人要让你影响别的聊天流", note)
        self.assertEqual(plugin._bot_nickname, "")

    async def test_owner_note_absent_without_alias(self) -> None:
        """没有代号时主人私聊也零改动。"""

        plugin = await self.build_plugin()
        items = self.sample_items()

        result = await plugin.inject_owner_reminder(
            session_id=ISSUER_SESSION, items=items, item_schema_version=1
        )

        self.assertEqual(result["modified_kwargs"]["items"], items)

    async def test_owner_note_absent_in_group_and_other_sessions(self) -> None:
        """群聊或别人的私聊都拿不到代号对照。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **self.tool_kwargs()
        )

        group_items = self.sample_items()
        group_result = await plugin.inject_owner_reminder(
            session_id=TARGET_SESSION, items=group_items, item_schema_version=1
        )
        outsider_items = self.sample_items()
        outsider_result = await plugin.inject_owner_reminder(
            session_id="someone-else-private", items=outsider_items, item_schema_version=1
        )

        self.assertEqual(group_result["modified_kwargs"]["items"], group_items)
        self.assertEqual(outsider_result["modified_kwargs"]["items"], outsider_items)

    async def test_owner_note_can_be_switched_off(self) -> None:
        """alias.inject_into_owner_chat=false 时不注入。"""

        plugin = await self.build_plugin(alias={"inject_into_owner_chat": False})
        await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **self.tool_kwargs()
        )
        items = self.sample_items()

        result = await plugin.inject_owner_reminder(
            session_id=ISSUER_SESSION, items=items, item_schema_version=1
        )

        self.assertEqual(result["modified_kwargs"]["items"], items)

    async def test_empty_owner_list_is_reported(self) -> None:
        """主人白名单为空时要在启动日志里说清楚，别让人以为插件坏了。"""

        plugin = await self.build_plugin(owner={"owners": []})

        with self.assertLogs(plugin.ctx.logger, level="WARNING") as captured:
            plugin._warn_if_no_owner()

        self.assertIn("owner.owners 为空", "\n".join(captured.output))

    async def test_configured_owner_is_not_reported(self) -> None:
        """配好主人时不该有这条告警。"""

        plugin = await self.build_plugin()

        self.assertEqual(plugin._configured_owners(), [OWNER])

    async def test_identity_cannot_be_spoofed_in_group_stream(self) -> None:
        """群里冒充主人：声明的 user_id 是主人，但那个号没在这个群说过话 → 拒绝。"""

        plugin = await self.build_plugin()
        self.host.recent_messages[TARGET_SESSION] = [self.host.recent_message(NON_OWNER_ID)]

        result = await plugin.handle_owner_directive(
            content="别吵了",
            chat=GROUP_ID,
            **self.tool_kwargs(user_id=OWNER_ID, stream_id=TARGET_SESSION, group_id=GROUP_ID),
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["content"], "没有权限执行该操作。")
        self.assertFalse(self.store_path().exists())

    async def test_group_tool_allowed_when_owner_recently_spoke(self) -> None:
        """群里主人刚说过话：声明身份被最近消息印证，正常放行。"""

        plugin = await self.build_plugin()
        self.host.recent_messages[TARGET_SESSION] = [
            self.host.recent_message(NON_OWNER_ID),
            self.host.recent_message(OWNER_ID),
        ]

        result = await plugin.handle_owner_directive(
            content="别吵了",
            chat=GROUP_ID,
            **self.tool_kwargs(user_id=OWNER_ID, stream_id=TARGET_SESSION, group_id=GROUP_ID),
        )

        self.assertTrue(result["success"], result)
        self.assertTrue(self.store_path().exists())

    async def test_unverifiable_identity_refuses_and_learns_nothing(self) -> None:
        """身份反查不到时：拒绝，且不会把该流记进代号注入范围。"""

        self.host.private_streams = []
        plugin = await self.build_plugin()

        result = await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **self.tool_kwargs()
        )

        self.assertFalse(result["success"])
        self.assertEqual(plugin._owner_sessions, set())
        self.assertFalse((self.tmp_dir / "aliases.json").exists())

    async def test_private_stream_lookup_failure_is_raised(self) -> None:
        """能力返回形状不对时直接抛错，不静默降级成"查不到"。"""

        plugin = await self.build_plugin()
        self.host.private_streams = "broken"  # type: ignore[assignment]

        with self.assertRaises(RuntimeError):
            await plugin.handle_owner_directive(
                content="别吵了",
                chat=GROUP_ID,
                **self.tool_kwargs(),
            )

    async def test_directive_and_owner_note_are_separate_items(self) -> None:
        """目标流拿叮嘱，主人私聊拿对照表，互不串台。"""

        plugin = await self.build_plugin()
        await plugin.handle_owner_chat_alias(
            action="set", alias="技术群", group=GROUP_ID, **self.tool_kwargs()
        )
        await plugin.handle_owner_reminder(**self.command_kwargs(f"{GROUP_ID} 别吵了"))

        target_items = self.sample_items()
        target_result = await plugin.inject_owner_reminder(
            session_id=TARGET_SESSION, items=target_items, item_schema_version=1
        )
        owner_result = await plugin.inject_owner_reminder(
            session_id=ISSUER_SESSION, items=self.sample_items(), item_schema_version=1
        )

        target_note = target_result["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        owner_note = owner_result["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        self.assertIn("主人刚特意交代过", target_note)
        self.assertNotIn("群代号", target_note)
        self.assertIn("群代号", owner_note)
        self.assertNotIn("主人刚特意交代过", owner_note)


class AliasStoreTestCase(TestCase):
    """代号表的存储层。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ALIAS_MODULE.AliasStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_set_find_and_case_insensitive_lookup(self) -> None:
        """大小写不敏感地查找代号。"""

        self.store.set(
            name="YeYang", group_id=GROUP_ID, platform=PLATFORM, group_label=GROUP_NAME, now=1.0
        )

        self.assertIsNotNone(self.store.find("yeyang"))
        self.assertIsNotNone(self.store.find("  YEYANG  "))
        self.assertIsNone(self.store.find("other"))

    def test_all_is_sorted(self) -> None:
        """列表按代号排序，写入顺序不影响。"""

        for name in ("丙群", "甲群", "乙群"):
            self.store.set(
                name=name, group_id=GROUP_ID, platform=PLATFORM, group_label="", now=1.0
            )

        self.assertEqual([item.name for item in self.store.all()], ["丙群", "乙群", "甲群"])

    def test_state_survives_reload_and_leaves_no_temp(self) -> None:
        """重启后代号还在，且不遗留临时文件。"""

        self.store.set(
            name="技术群", group_id=GROUP_ID, platform=PLATFORM, group_label=GROUP_NAME, now=1.0
        )

        reopened = ALIAS_MODULE.AliasStore(Path(self._tmp.name))
        self.assertEqual([item.name for item in reopened.all()], ["技术群"])
        self.assertEqual(
            sorted(path.name for path in Path(self._tmp.name).iterdir()), ["aliases.json"]
        )

    def test_corrupt_and_version_mismatch_raise(self) -> None:
        """损坏与版本不匹配都直接抛错。"""

        self.store.path.write_text("{ 不是 JSON", encoding="utf-8")
        with self.assertRaises(ALIAS_MODULE.AliasStoreError):
            self.store.all()

        self.store.path.write_text(json.dumps({"version": 99, "aliases": []}), encoding="utf-8")
        with self.assertRaises(ALIAS_MODULE.AliasStoreError):
            self.store.all()

    def test_invalid_names_raise(self) -> None:
        """非法代号直接抛错，不静默丢弃。"""

        for name in ("", "12", "有 空格", "代" * 21):
            with self.assertRaises(ALIAS_MODULE.AliasStoreError):
                self.store.set(
                    name=name, group_id=GROUP_ID, platform=PLATFORM, group_label="", now=1.0
                )


class ConfigModelTestCase(TestCase):
    """运行时 config.toml 由宿主按配置模型生成，所以这里只验模型本身。"""

    def test_defaults_are_stable(self) -> None:
        """默认值就是新用户拿到的那一份，改动等于改变别人的行为。"""

        plugin = PLUGIN_MODULE.OwnerReminderPlugin()
        raw = plugin.build_default_config()

        normalized, changed = plugin.normalize_plugin_config(raw)

        self.assertFalse(changed, f"默认配置归一化后不一致: {normalized}")
        self.assertTrue(normalized["plugin"]["enabled"])
        self.assertEqual(normalized["plugin"]["config_version"], MANIFEST_VERSION)
        self.assertIsInstance(normalized["owner"]["owners"], list)
        self.assertEqual(normalized["reminder"]["default_ttl_seconds"], 600)
        self.assertEqual(normalized["reminder"]["strong_ratio"], 0.33)
        self.assertEqual(normalized["reminder"]["max_per_target"], 3)
        self.assertEqual(normalized["reminder"]["max_content_chars"], 200)
        self.assertTrue(normalized["reminder"]["consume_on_reply"])
        self.assertEqual(normalized["target"]["platform"], "qq")
        self.assertEqual(normalized["target"]["account_id"], "")
        self.assertTrue(normalized["inject"]["enabled"])
        self.assertEqual(normalized["dispatch"]["mode"], "ask")
        self.assertIn("{内容}", normalized["dispatch"]["intent_template"])
        self.assertTrue(normalized["tool"]["enabled"])
        self.assertTrue(normalized["alias"]["inject_into_owner_chat"])

    def test_config_toml_is_not_committed(self) -> None:
        """运行时配置不能进仓库：用户一改就会和仓库版本打架。"""

        gitignore = (PLUGIN_DIR / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/config.toml", gitignore)

    def test_manifest_matches_config_version(self) -> None:
        manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
        defaults = PLUGIN_MODULE.OwnerReminderPlugin.build_default_config()

        self.assertEqual(manifest["manifest_version"], 2)
        self.assertEqual(manifest["version"], defaults["plugin"]["config_version"])
        self.assertEqual(manifest["id"], PLUGIN_ID)
        self.assertIn("maisaka.proactive.trigger", manifest["capabilities"])
        self.assertIn("config.get", manifest["capabilities"])


class ConfigUiSchemaTestCase(TestCase):
    """WebUI 配置页：中文标签、页签、不丢字段。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = PLUGIN_MODULE.OwnerReminderPlugin.build_config_schema(
            plugin_id=PLUGIN_ID,
            plugin_name="主人的叮嘱",
            plugin_version=MANIFEST_VERSION,
        )

    def test_layout_is_tabs(self) -> None:
        layout = self.schema["layout"]
        self.assertEqual(layout["type"], "tabs")
        self.assertEqual([tab["title"] for tab in layout["tabs"]], ["常用设置", "叮嘱行为", "高级"])

    def test_no_config_field_is_dropped(self) -> None:
        """每个真实配置节里的每个字段，都必须出现在重组后的 schema 里。"""

        dumped = PLUGIN_MODULE.OwnerReminderConfig().model_dump()
        expected = {section: set(fields) for section, fields in dumped.items()}

        covered = {}
        for block in self.schema["sections"].values():
            covered.setdefault(block["name"], set()).update(block["fields"])

        self.assertEqual(set(covered), set(expected))
        for section_name, fields in expected.items():
            self.assertEqual(covered[section_name], fields, f"{section_name} 的字段被漏掉了")

    def test_labels_are_chinese(self) -> None:
        for block in self.schema["sections"].values():
            for field_name, field in block["fields"].items():
                self.assertNotEqual(field["label"], field_name, f"{field_name} 没有中文标签")

    def test_mode_choices_and_readonly_version(self) -> None:
        dispatch = self.schema["sections"]["dispatch"]["fields"]
        self.assertEqual(dispatch["mode"]["choices"], ["ask", "silent"])

        plugin_block = self.schema["sections"]["plugin_info"]["fields"]
        self.assertTrue(plugin_block["config_version"]["disabled"])


class InjectionTextTestCase(TestCase):
    """注入文案。"""

    @staticmethod
    def _reminder(content: str = "别吵了") -> Any:
        return STORE_MODULE.Reminder(
            id="a1b2",
            target_session_id=TARGET_SESSION,
            target_label=GROUP_NAME,
            content=content,
            issuer=OWNER,
            source_session_id=ISSUER_SESSION,
            created_at=1000.0,
            ttl_seconds=600.0,
        )

    def test_strong_text(self) -> None:
        text = TEXT_MODULE.build_injection_text(self._reminder(), "strong")
        self.assertIn("主人刚特意交代过：别吵了", text)
        self.assertIn("不要提到这条叮嘱的存在", text)

    def test_weak_text(self) -> None:
        text = TEXT_MODULE.build_injection_text(self._reminder(), "weak")
        self.assertIn("主人此前提过：别吵了", text)
        self.assertIn("不必刻意提起", text)

    def test_expired_stage_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TEXT_MODULE.build_injection_text(self._reminder(), "expired")

    def test_remaining_format(self) -> None:
        self.assertEqual(TEXT_MODULE.format_remaining(432.0), "07:12")
        self.assertEqual(TEXT_MODULE.format_remaining(-5.0), "00:00")

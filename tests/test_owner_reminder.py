"""主人的叮嘱插件单元测试。

用标准库 unittest，不依赖 pytest。插件模块按 Runner 的方式（importlib + 子模块搜索路径）
加载，这样 plugin.py 里的相对导入才可用。

注意：这里不导入宿主 ``src.*``（那会连带启动整个 MaiBot），
注入项与宿主反序列化器的契约校验见 README「尚未验证」一节的手工步骤。
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
import tomllib

from maibot_sdk import PluginContext, PluginPaths
from maibot_sdk.types import ComponentType

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = "yeye_owner_reminder"

PLATFORM = "qq"
OWNER_ID = "1215349250"
OWNER = f"{PLATFORM}:{OWNER_ID}"
NON_OWNER_ID = "99999999"
GROUP_ID = "943355126"
GROUP_NAME = "全是熟人社会，夜羊烂完了"
TARGET_SESSION = "ad234af79b55716264cd386a4770b805"
ISSUER_SESSION = "3695a82e4db39ede9f14040523ab13b4"
OTHER_ACCOUNT = "3350829885"
DUPLICATE_SESSION = "be4a61f2a0928bd3d392ccf8d36f2d26"

COMMAND_NAME = "owner_reminder"


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
                "user_nickname": "42yoru",
                "user_cardname": "",
                "account_id": "10001",
                "platform": PLATFORM,
            }
        ]
        self.group_streams_error: Optional[str] = None
        self.sent: List[Tuple[str, str]] = []
        self.proactive_calls: List[Dict[str, Any]] = []
        self.proactive_result: Dict[str, Any] = {
            "success": True,
            "queued": True,
            "task_id": "proactive:yeye.owner-reminder:1",
        }
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
            return self.proactive_result
        raise AssertionError(f"未预期的能力调用: {capability}")

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
            plugin_id="yeye.owner-reminder",
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
        """构造插件配置字典；只覆盖传入的配置节。"""

        config: Dict[str, Any] = {"plugin": {"enabled": True, "config_version": "0.1.0"}}
        config.update(sections)
        return config

    @staticmethod
    def command_kwargs(
        rest: str,
        *,
        user_id: str = OWNER_ID,
        group_id: str = "",
    ) -> Dict[str, Any]:
        """构造命令调用参数，忠实模拟宿主捕获的 ``rest``（带前导空格）。"""

        captured_rest = f" {rest}" if rest else ""
        return {
            "text": f"/叮嘱{captured_rest}",
            "stream_id": ISSUER_SESSION,
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

    async def test_owner_whitelist_silently_ignores_others(self) -> None:
        """非主人下达：不回执、不落盘。"""

        plugin = await self.build_plugin()

        success, _, intercept = await plugin.handle_owner_reminder(
            **self.command_kwargs(f"{GROUP_ID} 别吵了", user_id=NON_OWNER_ID)
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
        self.assertEqual(saved["target_label"], "42yoru的私聊")

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
            plugin_id="yeye.owner-reminder",
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
            pattern.search("/叮嘱 943355126 别吵了").groupdict()["rest"], " 943355126 别吵了"
        )
        self.assertEqual(pattern.search("/influence list").groupdict()["rest"], " list")
        self.assertIsNone(pattern.search("前缀 /叮嘱 别吵了"))
        # 别把 /influencer 这种同前缀的普通消息吞成命令
        self.assertIsNone(pattern.search("/influencer 你好"))
        self.assertIsNone(pattern.search("/influence2 你好"))

    def test_hook_is_declared(self) -> None:
        """planner hook 是 blocking + late。"""

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


class ArgumentParseTestCase(TestCase):
    """命令参数解析。"""

    def test_set_with_group_id(self) -> None:
        parsed = ARGS_MODULE.parse_command("943355126 别吵了")
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.action, ARGS_MODULE.ACTION_SET)
        self.assertEqual(parsed.target_group_id, "943355126")
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
        parsed = ARGS_MODULE.parse_command("943355126 第一行\n第二行")
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
        self.assertFalse(ARGS_MODULE.parse_command("943355126").ok)

    def test_revoke_all_markers(self) -> None:
        self.assertTrue(ARGS_MODULE.is_revoke_all("all"))
        self.assertTrue(ARGS_MODULE.is_revoke_all("全部"))
        self.assertFalse(ARGS_MODULE.is_revoke_all("a1b2"))


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


class ConfigFileTestCase(TestCase):
    """config.toml 必须与配置模型一致，否则宿主会在加载时自动改写它。"""

    def test_config_toml_matches_model(self) -> None:
        raw = tomllib.loads((PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
        plugin = PLUGIN_MODULE.OwnerReminderPlugin()

        normalized, changed = plugin.normalize_plugin_config(raw)

        self.assertFalse(changed, f"config.toml 与配置模型不一致: {normalized}")
        self.assertTrue(normalized["plugin"]["enabled"])
        self.assertEqual(normalized["plugin"]["config_version"], "0.1.0")
        self.assertEqual(normalized["owner"]["owners"], ["qq:1215349250"])
        self.assertEqual(normalized["reminder"]["default_ttl_seconds"], 600)
        self.assertEqual(normalized["reminder"]["strong_ratio"], 0.33)
        self.assertEqual(normalized["reminder"]["max_per_target"], 3)
        self.assertEqual(normalized["reminder"]["max_content_chars"], 200)
        self.assertEqual(normalized["target"]["platform"], "qq")
        self.assertEqual(normalized["target"]["account_id"], "")
        self.assertTrue(normalized["inject"]["enabled"])
        self.assertEqual(normalized["dispatch"]["mode"], "ask")
        self.assertIn("{内容}", normalized["dispatch"]["intent_template"])

    def test_manifest_matches_config_version(self) -> None:
        manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
        raw = tomllib.loads((PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))

        self.assertEqual(manifest["manifest_version"], 2)
        self.assertEqual(manifest["version"], raw["plugin"]["config_version"])
        self.assertEqual(manifest["id"], "yeye.owner-reminder")
        self.assertIn("maisaka.proactive.trigger", manifest["capabilities"])


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

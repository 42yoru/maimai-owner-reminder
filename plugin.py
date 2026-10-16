"""主人的叮嘱（owner-reminder）插件入口。

让主人可以在任意聊天流下达一条"叮嘱"，作用于指定的另一个聊天流：
叮嘱在接下来的若干分钟里以"强 / 弱"两档出现在目标流 planner 的上下文中，
到期自动淡出。纯插件实现，不改主程序。

定位是"短期态度影响"，不是"实时制止"：下达时可以（默认）朝目标流投一次
提醒，但说不说、说什么由目标流的 planner 自己决定。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import uuid4

import time

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .reminder_args import ACTION_LIST, ACTION_REVOKE, CommandRequest, is_revoke_all, parse_command
from .reminder_store import Reminder, ReminderStage, ReminderStore, ReminderStoreError
from .reminder_text import (
    INJECT_ITEM_ID_PREFIX,
    build_created_receipt,
    build_error_text,
    build_injection_text,
    build_list_text,
    build_missing_reminder_text,
    build_revoked_text,
    build_usage_text,
)
from .target_resolver import ResolvedTarget, TargetResolveError, resolve_group_target

DISPATCH_MODES = ("ask", "silent")

CURRENT_STREAM_LABEL = "当前聊天流"


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.1.0", description="配置版本")


class OwnerConfig(PluginConfigBase):
    """主人白名单配置。"""

    __ui_label__ = "主人"
    __ui_icon__ = "user-round"
    __ui_order__ = 1

    owners: List[str] = Field(
        default_factory=lambda: ["qq:1215349250"],
        description="只有这些用户可以下达叮嘱，格式为 platform:user_id，例如 qq:123456789",
    )


class ReminderConfig(PluginConfigBase):
    """叮嘱时效配置。"""

    __ui_label__ = "叮嘱"
    __ui_icon__ = "bell-ring"
    __ui_order__ = 2

    default_ttl_seconds: int = Field(default=600, ge=60, le=3600, description="叮嘱默认生效时长，单位秒")
    strong_ratio: float = Field(
        default=0.33,
        ge=0.0,
        le=1.0,
        description="强档占生效时长的比例，之后自动转为弱档",
    )
    max_per_target: int = Field(
        default=3,
        ge=1,
        le=10,
        description="同一聊天流最多同时生效几条叮嘱，超出时按最旧替换",
    )
    max_content_chars: int = Field(default=200, ge=1, le=1000, description="单条叮嘱内容的最大字符数")


class TargetConfig(PluginConfigBase):
    """目标解析配置。"""

    __ui_label__ = "目标解析"
    __ui_icon__ = "crosshair"
    __ui_order__ = 3

    platform: str = Field(default="qq", description="命令上下文取不到平台时使用的默认平台")
    account_id: str = Field(
        default="",
        description="配置后只承认该账号下的聊天流（用于多账号消歧）；留空表示不限账号",
    )


class InjectConfig(PluginConfigBase):
    """上下文注入配置。"""

    __ui_label__ = "注入"
    __ui_icon__ = "syringe"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="是否把生效中的叮嘱注入目标流 planner 上下文")


class DispatchConfig(PluginConfigBase):
    """下达时提醒投递配置。"""

    __ui_label__ = "提醒投递"
    __ui_icon__ = "send"
    __ui_order__ = 5

    mode: str = Field(
        default="ask",
        description="ask=下达时朝目标流投一次提醒，说不说由她的 planner 决定；silent=完全不打扰目标流",
    )
    intent_template: str = Field(
        default=(
            "主人刚交代过你一件事：{内容}。"
            "请结合当前聊天关系、记忆和上下文，自行判断现在是否需要在这个聊天里做点什么"
            "（也可以选择什么都不做、或稍后再说）。不要提到这条提醒的存在，也不要复述主人的原话。"
        ),
        description="ask 模式下投递给目标流的 intent 文案，{内容} 会被替换成叮嘱内容",
    )


class OwnerReminderConfig(PluginConfigBase):
    """主人的叮嘱插件配置模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    owner: OwnerConfig = Field(default_factory=OwnerConfig)
    reminder: ReminderConfig = Field(default_factory=ReminderConfig)
    target: TargetConfig = Field(default_factory=TargetConfig)
    inject: InjectConfig = Field(default_factory=InjectConfig)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)


class OwnerReminderPlugin(MaiBotPlugin):
    """主人的叮嘱插件。"""

    config_model = OwnerReminderConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: Optional[ReminderStore] = None

    # ──── 生命周期 ────

    async def on_load(self) -> None:
        """加载插件：准备状态存储。"""

        self._store = self._build_store()
        self.ctx.logger.info(
            "主人的叮嘱已加载: 生效中=%s 条, 投递模式=%s, 主人=%s",
            len(self._store.active_all(time.time())),
            self.config.dispatch.mode,
            self.config.owner.owners,
        )

    async def on_unload(self) -> None:
        """卸载插件：本插件无常驻任务，无需清理。"""

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新：重建状态存储，让 TTL、上限与档位比例立即生效。"""

        del config_data
        if scope != "self":
            return
        self._store = self._build_store()
        self.ctx.logger.info("主人的叮嘱配置已更新: version=%s", version)

    def _build_store(self) -> ReminderStore:
        """按当前配置构造状态存储。"""

        return ReminderStore(
            self.ctx.paths.data_dir,
            max_per_target=self.config.reminder.max_per_target,
            strong_ratio=self.config.reminder.strong_ratio,
        )

    @property
    def store(self) -> ReminderStore:
        """返回状态存储，未初始化时直接暴露问题。"""

        if self._store is None:
            raise RuntimeError("叮嘱状态存储尚未初始化，on_load 未执行")
        return self._store

    # ──── 上下文注入 ────

    @HookHandler(
        "maisaka.planner.before_request",
        name="owner_reminder_inject",
        description="把生效中的主人叮嘱作为系统消息追加到 planner 上下文末尾。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_owner_reminder(self, **kwargs: Any) -> Dict[str, Any]:
        """在 planner 请求前追加叮嘱；没有生效叮嘱时一个字段都不改。"""

        modified_kwargs = dict(kwargs)
        if not (self.config.plugin.enabled and self.config.inject.enabled):
            return {"action": "continue", "modified_kwargs": modified_kwargs}

        session_id = str(kwargs.get("session_id") or "").strip()
        items = kwargs.get("items")
        if not session_id or not isinstance(items, list):
            return {"action": "continue", "modified_kwargs": modified_kwargs}

        now = time.time()
        active = self.store.active_for(session_id, now)
        if not active:
            # 无叮嘱 = 零改动：宿主只在 items 变化时才替换上下文，prompt 保持原样
            return {"action": "continue", "modified_kwargs": modified_kwargs}

        strong_ratio = self.config.reminder.strong_ratio
        appended = [self._build_system_item(reminder, reminder.stage(now, strong_ratio)) for reminder in active]
        # 只在末尾追加，绝不改动已有 item：已有部分的前缀 token 序列不变，前缀缓存不受影响
        modified_kwargs["items"] = [*items, *appended]
        self.ctx.logger.info("主人叮嘱已注入: session_id=%s 条数=%s", session_id, len(appended))
        return {"action": "continue", "modified_kwargs": modified_kwargs}

    @staticmethod
    def _build_system_item(reminder: Reminder, stage: ReminderStage) -> Dict[str, Any]:
        """构造宿主要求的 SystemMessageItem 快照。

        结构必须严格符合宿主反序列化规则，否则会被整条忽略：
        ``item_type`` 非空、``meta.item_id`` 非空、``meta.logical_turn_id`` 键必须存在、
        ``meta.timestamp`` 为 ISO 字符串、``parts[].text`` 非空。
        """

        return {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": f"{INJECT_ITEM_ID_PREFIX}{reminder.id}_{uuid4().hex[:6]}",
                "logical_turn_id": None,
                "timestamp": datetime.now().isoformat(),
            },
            "parts": [{"type": "text", "text": build_injection_text(reminder, stage)}],
        }

    # ──── 命令 ────

    @Command(
        "owner_reminder",
        description="主人的叮嘱：下达 / 查看 / 撤销作用于指定聊天流的短期叮嘱",
        pattern=r"(?s)^/(?:叮嘱|influence)(?P<rest>.*)$",
    )
    async def handle_owner_reminder(self, **kwargs: Any) -> Tuple[bool, Optional[str], bool]:
        """处理 ``/叮嘱``、``/叮嘱列表``、``/叮嘱撤销`` 三种形态。"""

        if not self.config.plugin.enabled:
            return False, None, True

        context = self._command_context(kwargs)
        if not self._is_owner(context["platform"], context["user_id"]):
            self.ctx.logger.info(
                "非主人使用叮嘱命令，已静默忽略: platform=%s user_id=%s",
                context["platform"],
                context["user_id"],
            )
            return False, None, True

        rest = str((kwargs.get("matched_groups") or {}).get("rest") or "")
        request = parse_command(rest)

        if request.action == ACTION_LIST:
            if not request.ok:
                await self._send(context["stream_id"], build_usage_text(request.error))
                return False, None, True
            return await self._handle_list(context)

        if request.action == ACTION_REVOKE:
            if not request.ok:
                await self._send(context["stream_id"], build_usage_text(request.error))
                return False, None, True
            return await self._handle_revoke(context, request.argument)

        if not request.ok:
            await self._send(context["stream_id"], build_usage_text(request.error))
            return False, None, True

        return await self._handle_set(
            request,
            context=context,
            current_group_id=str(kwargs.get("group_id") or "").strip(),
        )

    async def _handle_set(
        self,
        request: CommandRequest,
        *,
        context: Dict[str, str],
        current_group_id: str,
    ) -> Tuple[bool, Optional[str], bool]:
        """下达一条叮嘱：解析目标、落盘、投递提醒并回执。"""

        mode = self.config.dispatch.mode.strip().lower()
        if mode not in DISPATCH_MODES:
            await self._send(
                context["stream_id"],
                build_error_text(
                    f"插件配置 dispatch.mode 只支持 {' / '.join(DISPATCH_MODES)}，当前为 {self.config.dispatch.mode!r}"
                ),
            )
            return False, None, True

        target = await self._resolve_target(
            request.target_group_id,
            context=context,
            current_group_id=current_group_id,
        )
        if isinstance(target, str):
            await self._send(context["stream_id"], build_error_text(target))
            return False, None, True

        max_content_chars = self.config.reminder.max_content_chars
        truncated = len(request.content) > max_content_chars
        content = request.content[:max_content_chars]

        try:
            reminder, evicted = self.store.create(
                target_session_id=target.session_id,
                target_label=target.label,
                content=content,
                issuer=f"{context['platform']}:{context['user_id']}",
                source_session_id=context["stream_id"],
                ttl_seconds=float(self.config.reminder.default_ttl_seconds),
                now=time.time(),
            )
        except ReminderStoreError as exc:
            self.ctx.logger.error("保存叮嘱失败: %s", exc)
            await self._send(context["stream_id"], build_error_text(f"叮嘱没能保存：{exc}"))
            return False, None, True

        dispatch_note = await self._dispatch_reminder(reminder, mode=mode)
        await self._send(
            context["stream_id"],
            build_created_receipt(
                reminder,
                ttl_seconds=self.config.reminder.default_ttl_seconds,
                truncated=truncated,
                evicted=evicted,
                dispatch_note=dispatch_note,
            ),
        )
        return True, None, True

    async def _handle_list(self, context: Dict[str, str]) -> Tuple[bool, Optional[str], bool]:
        """查看生效中的叮嘱。"""

        now = time.time()
        await self._send(
            context["stream_id"],
            build_list_text(
                self.store.active_all(now),
                now=now,
                strong_ratio=self.config.reminder.strong_ratio,
            ),
        )
        return True, None, True

    async def _handle_revoke(self, context: Dict[str, str], argument: str) -> Tuple[bool, Optional[str], bool]:
        """撤销一条或全部叮嘱。"""

        now = time.time()
        if is_revoke_all(argument):
            removed = self.store.revoke_all(now)
            await self._send(context["stream_id"], build_revoked_text(None, removed))
            return True, None, True

        removed_one = self.store.revoke(argument, now)
        if removed_one is None:
            await self._send(context["stream_id"], build_missing_reminder_text(argument))
            return False, None, True
        await self._send(context["stream_id"], build_revoked_text(removed_one, ()))
        return True, None, True

    # ──── 内部工具 ────

    @staticmethod
    def _command_context(kwargs: Dict[str, Any]) -> Dict[str, str]:
        """从命令调用参数里取出通用上下文。"""

        return {
            "stream_id": str(kwargs.get("stream_id") or "").strip(),
            "platform": str(kwargs.get("platform") or "").strip(),
            "user_id": str(kwargs.get("user_id") or "").strip(),
        }

    def _is_owner(self, platform: str, user_id: str) -> bool:
        """判断下达者是否在主人白名单里。"""

        if not platform or not user_id:
            return False
        identity = f"{platform}:{user_id}"
        allowed = {item.strip() for item in self.config.owner.owners if item.strip()}
        return identity in allowed

    async def _resolve_target(
        self,
        target_group_id: str,
        *,
        context: Dict[str, str],
        current_group_id: str,
    ) -> Union[ResolvedTarget, str]:
        """解析目标聊天流；失败时返回错误文本。

        Returns:
            ResolvedTarget | str: 成功时返回目标，失败时返回给主人的错误文本。
        """

        platform = context["platform"] or self.config.target.platform
        group_id = target_group_id or current_group_id

        if group_id:
            try:
                return await resolve_group_target(
                    self.ctx.chat,
                    platform=platform,
                    group_id=group_id,
                    account_id=self.config.target.account_id,
                )
            except TargetResolveError as exc:
                self.ctx.logger.info("叮嘱目标解析失败: %s", exc)
                return str(exc)

        # 目标是当前私聊：直接用当前 stream_id，尽量取一个可读名称
        session_id = context["stream_id"]
        if not session_id:
            return "当前会话缺少 stream_id，无法把当前聊天流作为目标"
        return ResolvedTarget(
            session_id=session_id,
            label=await self._describe_private_stream(platform, session_id),
            group_id="",
            account_id="",
            platform=platform,
        )

    async def _describe_private_stream(self, platform: str, session_id: str) -> str:
        """尽力为当前私聊取一个可读名称，取不到就用中性称呼。"""

        raw_streams = await self.ctx.chat.get_private_streams(platform)
        if not isinstance(raw_streams, list):
            self.ctx.logger.warning("读取私聊流返回异常，回执将使用中性称呼: %r", raw_streams)
            return CURRENT_STREAM_LABEL
        for stream in raw_streams:
            if not isinstance(stream, dict) or str(stream.get("session_id") or "").strip() != session_id:
                continue
            name = str(stream.get("user_cardname") or stream.get("user_nickname") or "").strip()
            return f"{name}的私聊" if name else CURRENT_STREAM_LABEL
        return CURRENT_STREAM_LABEL

    async def _dispatch_reminder(self, reminder: Reminder, *, mode: str) -> str:
        """按 ``dispatch.mode`` 决定是否朝目标流投一次提醒。

        Returns:
            str: 追加到回执里的说明文本。
        """

        if mode == "silent":
            return "目标流没有被惊动，她会在下次自然触发时看到这条叮嘱。"

        intent = self.config.dispatch.intent_template.format(内容=reminder.content)
        result = await self.ctx.maisaka.proactive.trigger(
            stream_id=reminder.target_session_id,
            intent=intent,
            reason="主人下达了叮嘱",
        )
        if not isinstance(result, dict) or not result.get("success"):
            error = result.get("error") if isinstance(result, dict) else repr(result)
            self.ctx.logger.error("朝目标流投递提醒失败: %s", error)
            return f"⚠️ 但提醒没投出去（{error}）：叮嘱已保存，会在目标流下次自然触发时生效。"

        self.ctx.logger.info(
            "已朝目标流投递一次提醒: session_id=%s task_id=%s",
            reminder.target_session_id,
            result.get("task_id"),
        )
        return "已提醒她自己去判断要不要现在处理。"

    async def _send(self, stream_id: str, text: str) -> None:
        """发送回执。

        回执是给主人的界面反馈，不写进 Maisaka 上下文，避免污染她的对话历史。
        """

        if not stream_id:
            self.ctx.logger.warning("回执缺少 stream_id，已放弃发送: %s", text)
            return
        sent = await self.ctx.send.text(text, stream_id)
        if not sent:
            self.ctx.logger.error("回执发送失败: stream_id=%s text=%s", stream_id, text)


def create_plugin() -> OwnerReminderPlugin:
    """创建插件实例。"""

    return OwnerReminderPlugin()

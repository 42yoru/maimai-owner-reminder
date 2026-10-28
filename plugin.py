"""主人的叮嘱（owner-reminder）插件入口。

让主人可以在任意聊天流下达一条"叮嘱"，作用于指定的另一个聊天流：
叮嘱在接下来的若干分钟里以"强 / 弱"两档出现在目标流 planner 的上下文中，
到期自动淡出。纯插件实现，不改主程序。

定位是"短期态度影响"，不是"实时制止"：下达时可以（默认）朝目标流投一次
提醒，但说不说、说什么由目标流的 planner 自己决定。

两条下达通道共用同一套落盘 / 投递 / 注入链路：
- 命令 ``/叮嘱``（确定性，可兜底）；
- 工具 ``owner_directive``（自然语言，由她自己的 planner 识别意图后调用）。
群可以起代号（``owner_chat_alias`` 工具维护），下达时用代号指代。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from uuid import uuid4

import asyncio
import time

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .alias_store import AliasStore, AliasStoreError
from .config_ui import build_config_ui_schema
from .reminder_args import ACTION_LIST, ACTION_REVOKE, CommandRequest, is_revoke_all, parse_command
from .reminder_store import Reminder, ReminderStage, ReminderStore, ReminderStoreError
from .reminder_text import (
    INJECT_ITEM_ID_PREFIX,
    OWNER_NOTE_ITEM_PREFIX,
    build_alias_list_text,
    build_alias_missing_text,
    build_alias_removed_text,
    build_alias_saved_text,
    build_created_receipt,
    build_error_text,
    build_injection_text,
    build_list_text,
    build_missing_content_text,
    build_missing_reminder_text,
    build_not_owner_text,
    build_owner_alias_note,
    build_revoked_text,
    build_tool_disabled_text,
    build_unknown_alias_action_text,
    build_unknown_alias_text,
    build_usage_text,
)
from .target_resolver import ResolvedTarget, TargetResolveError, resolve_group_target

DISPATCH_MODES = ("ask", "silent")

CURRENT_STREAM_LABEL = "当前聊天流"

ALIAS_ACTION_LIST = ("", "list", "ls", "列表", "查看")
ALIAS_ACTION_SET = ("set", "add", "设置", "新增")
ALIAS_ACTION_REMOVE = ("remove", "delete", "del", "删除")

IDENTITY_RECENT_SCAN = 50
"""群聊流核对声明身份时回看的消息条数；只用于身份核验，与叮嘱内容无关。"""


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.4.11", description="配置版本")


class OwnerConfig(PluginConfigBase):
    """主人白名单配置。"""

    __ui_label__ = "主人"
    __ui_icon__ = "user-round"
    __ui_order__ = 1

    owners: List[str] = Field(
        default_factory=list,
        description="只有这些用户可以下达叮嘱，格式为 platform:user_id，例如 qq:123456789；留空表示谁都不能下达",
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
    consume_on_reply: bool = Field(
        default=True,
        description=(
            "投递过提醒的叮嘱，在她于目标流发出第一条可见回复后立即消费掉（不再等 TTL）；"
            "关掉则回到纯 TTL 行为。silent 模式没有投递，不受此项影响"
        ),
    )


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


class ToolConfig(PluginConfigBase):
    """自然语言工具配置。"""

    __ui_label__ = "自然语言工具"
    __ui_icon__ = "wrench"
    __ui_order__ = 6

    enabled: bool = Field(
        default=True,
        description="是否允许 owner_directive / owner_chat_alias 工具执行（关掉后工具仍在工具表里，但一律拒绝）",
    )


class AliasConfig(PluginConfigBase):
    """群代号配置。"""

    __ui_label__ = "群代号"
    __ui_icon__ = "tags"
    __ui_order__ = 7

    inject_into_owner_chat: bool = Field(
        default=True,
        description="是否在主人私聊里注入群代号对照表（仅当已存在代号时注入；无代号时零改动）",
    )


class OwnerReminderConfig(PluginConfigBase):
    """主人的叮嘱插件配置模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    owner: OwnerConfig = Field(default_factory=OwnerConfig)
    reminder: ReminderConfig = Field(default_factory=ReminderConfig)
    target: TargetConfig = Field(default_factory=TargetConfig)
    inject: InjectConfig = Field(default_factory=InjectConfig)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    tool: ToolConfig = Field(default_factory=ToolConfig)
    alias: AliasConfig = Field(default_factory=AliasConfig)


class OwnerReminderPlugin(MaiBotPlugin):
    """主人的叮嘱插件。"""

    config_model = OwnerReminderConfig

    @classmethod
    def build_config_schema(cls, **plugin_info: Any) -> Dict[str, Any]:
        """只重组 WebUI 的展示结构，配置模型与 TOML 路径保持不变。"""

        return build_config_ui_schema(super().build_config_schema(**plugin_info))

    def __init__(self) -> None:
        super().__init__()
        self._store: Optional[ReminderStore] = None
        self._alias_store: Optional[AliasStore] = None
        self._owner_sessions: set[str] = set()
        self._bot_nickname: str = ""

    # ──── 组件声明 ────

    def get_components(self) -> List[Dict[str, Any]]:
        """收集组件，并把工具上的作用域声明提到顶层。

        Runner 读取的是**顶层** `chat_scope`（`runner_main.py:1420`），而 SDK 的
        `collect_components` 只对 ReplyExtension 做这个提升。所以 `@Tool(..., chat_scope="private")`
        必须在这里补一次，否则会被当成 `all`——群聊里也会看到这两个工具。
        """

        components = super().get_components()
        for component in components:
            metadata = component.get("metadata")
            if not isinstance(metadata, dict):
                continue
            chat_scope = str(metadata.get("chat_scope") or "").strip()
            if chat_scope and "chat_scope" not in component:
                component["chat_scope"] = chat_scope
        return components

    # ──── 生命周期 ────

    async def on_load(self) -> None:
        """加载插件：准备状态存储，并尽力认领主人的私聊会话。"""

        self._store = self._build_store()
        self._alias_store = AliasStore(self.ctx.paths.data_dir)
        # 主动读一次状态文件：坏文件在这里就暴露（而不是等第一条叮嘱进来才发现）。
        # 别让它只是格式化日志的副作用——那样"读状态"这件事就没有明确责任人了。
        active_count = len(self._store.active_all(time.time()))
        await self._refresh_owner_sessions()
        await self._refresh_bot_nickname()
        self.ctx.logger.info(
            "主人的叮嘱已加载: 生效中=%s 条, 代号=%s 个, 主人的私聊=%s, 投递模式=%s, 昵称=%s",
            active_count,
            len(self._alias_store.all()),
            sorted(self._owner_sessions),
            self.config.dispatch.mode,
            self._bot_nickname or "(未知)",
        )
        self._warn_if_misconfigured()

    def _configured_owners(self) -> List[str]:
        """返回归一化后的主人白名单（去掉空白项）。"""

        return [item.strip() for item in self.config.owner.owners if item.strip()]

    def _warn_if_no_owner(self) -> None:
        """主人白名单为空或格式不对时给明确提示，免得用户以为插件没装上。"""

        owners = self._configured_owners()
        if not owners:
            self.ctx.logger.warning(
                "owner.owners 为空：还没有任何人可以下达叮嘱。"
                "请在 config.toml 的 [owner] 里填上 platform:user_id（例如 qq:123456789），保存后立即生效。"
            )
            return
        # 格式不对的条目永远匹配不上，属于「填了却没权限」，必须在加载时就喊出来
        broken = [item for item in owners if not _split_owner(item)]
        if broken:
            self.ctx.logger.warning(
                "owner.owners 里有 %s 个条目格式不对，这些条目不会生效: %s。"
                "正确写法是 platform:user_id（英文冒号，例如 qq:123456789）。",
                len(broken),
                broken,
            )

    def _warn_if_intent_template_is_broken(self) -> None:
        """ask 模式下模板必须带 {内容}，否则投递出去的指令里根本没有叮嘱本身。"""

        template = self.config.dispatch.intent_template
        if "{内容}" in template:
            return
        self.ctx.logger.warning(
            "dispatch.intent_template 里没有 {内容} 占位符：ask 模式投递出去的指令不会带上叮嘱内容，"
            "目标流只会收到一句空的「做点什么」。请在 config.toml 的 [dispatch] 一节补上 {内容}。"
        )

    def _warn_if_misconfigured(self) -> None:
        """加载与热更新时统一做一遍配置体检。"""

        self._warn_if_no_owner()
        self._warn_if_intent_template_is_broken()

    async def on_unload(self) -> None:
        """卸载插件：本插件无常驻任务，无需清理。"""

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新：重建状态存储，让 TTL、上限、开关立即生效。"""

        del config_data
        if scope != "self":
            return
        self._store = self._build_store()
        self._alias_store = AliasStore(self.ctx.paths.data_dir)
        await self._refresh_owner_sessions()
        await self._refresh_bot_nickname()
        self.ctx.logger.info("主人的叮嘱配置已更新: version=%s", version)
        self._warn_if_misconfigured()

    def _build_store(self) -> ReminderStore:
        """按当前配置构造叮嘱状态存储。"""

        return ReminderStore(
            self.ctx.paths.data_dir,
            max_per_target=self.config.reminder.max_per_target,
            strong_ratio=self.config.reminder.strong_ratio,
        )

    @property
    def store(self) -> ReminderStore:
        """返回叮嘱状态存储，未初始化时直接暴露问题。"""

        if self._store is None:
            raise RuntimeError("叮嘱状态存储尚未初始化，on_load 未执行")
        return self._store

    @property
    def alias_store(self) -> AliasStore:
        """返回群代号表，未初始化时直接暴露问题。"""

        if self._alias_store is None:
            raise RuntimeError("群代号表尚未初始化，on_load 未执行")
        return self._alias_store

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
        """在 planner 请求前追加叮嘱；没有任何要注入的内容时一个字段都不改。"""

        modified_kwargs = dict(kwargs)
        if not (self.config.plugin.enabled and self.config.inject.enabled):
            return {"action": "continue", "modified_kwargs": modified_kwargs}

        session_id = str(kwargs.get("session_id") or "").strip()
        items = kwargs.get("items")
        if not session_id or not isinstance(items, list):
            return {"action": "continue", "modified_kwargs": modified_kwargs}

        now = time.time()
        strong_ratio = self.config.reminder.strong_ratio
        appended: List[Dict[str, Any]] = [
            self._build_system_item(reminder, reminder.stage(now, strong_ratio))
            # 状态文件是同步 JSON 读写，放在 hook 里会直接占住事件循环；
            # 注入与消费是每条消息都会走到的热路径，必须挪到线程里
            for reminder in await asyncio.to_thread(self.store.active_for, session_id, now)
        ]
        owner_note = self._build_owner_note(session_id)
        if owner_note is not None:
            appended.append(owner_note)

        if not appended:
            # 没有要注入的内容 = 零改动：宿主只在 items 变化时才替换上下文，prompt 保持原样
            return {"action": "continue", "modified_kwargs": modified_kwargs}

        # 只在末尾追加，绝不改动已有 item：已有部分的前缀 token 序列不变，前缀缓存不受影响
        modified_kwargs["items"] = [*items, *appended]
        self.ctx.logger.info("主人叮嘱已注入: session_id=%s 条数=%s", session_id, len(appended))
        return {"action": "continue", "modified_kwargs": modified_kwargs}

    # ──── 兑现即消费 ────

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="owner_reminder_consume",
        description="目标流即将发出可见回复时，消费掉已经投递过提醒的叮嘱。",
        # 它不改任何 kwargs、也不需要拦下消息，用 OBSERVE 就够了；
        # BLOCKING 会跟着把整条发送链路挂在自己的超时上
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def consume_after_reply(self, **kwargs: Any) -> Dict[str, Any]:
        """她真的在目标流说话了，这条叮嘱的作用已经兑现，立即撤掉。

        消费点必须落在"可见回复即将发出"这一刻，不能落在"投递"上：
        ``ask`` 只是入队唤醒，叮嘱注入发生在 planner 请求**之前**，
        投递完就删等于让那一轮 planner 看不到叮嘱——白投。

        不修改任何 kwargs，只做旁路清理。
        """

        if not (self.config.plugin.enabled and self.config.reminder.consume_on_reply):
            return {"action": "continue"}

        session_id = str(kwargs.get("session_id") or "").strip()
        if not session_id:
            return {"action": "continue"}

        try:
            consumed = await asyncio.to_thread(self.store.consume_dispatched, session_id, time.time())
        except ReminderStoreError as exc:
            # 状态文件出问题时宁可不消费，也不能让一次回复因此失败
            self.ctx.logger.error("消费叮嘱失败: %s", exc)
            return {"action": "continue"}

        if consumed:
            self.ctx.logger.info(
                "叮嘱已兑现并消费: session_id=%s ids=%s",
                session_id,
                [item.id for item in consumed],
            )
        return {"action": "continue"}

    @classmethod
    def _build_system_item(cls, reminder: Reminder, stage: ReminderStage) -> Dict[str, Any]:
        """构造一条注入的 SystemMessageItem。"""

        return cls._build_text_item(build_injection_text(reminder, stage), INJECT_ITEM_ID_PREFIX)

    @staticmethod
    def _build_text_item(text: str, item_id_prefix: str) -> Dict[str, Any]:
        """构造宿主要求的 SystemMessageItem 快照。

        结构必须严格符合宿主反序列化规则，否则会被整条忽略：
        ``item_type`` 非空、``meta.item_id`` 非空、``meta.logical_turn_id`` 键必须存在、
        ``meta.timestamp`` 为 ISO 字符串、``parts[].text`` 非空。
        """

        return {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": f"{item_id_prefix}{uuid4().hex[:10]}",
                "logical_turn_id": None,
                "timestamp": datetime.now().isoformat(),
            },
            "parts": [{"type": "text", "text": text}],
        }

    def _build_owner_note(self, session_id: str) -> Optional[Dict[str, Any]]:
        """在主人的私聊里补一条群代号对照 + 工具提示；不满足条件时返回 None。"""

        if not (self.config.plugin.enabled and self.config.inject.enabled):
            return None
        if not (self.config.tool.enabled and self.config.alias.inject_into_owner_chat):
            return None
        if session_id not in self._owner_sessions:
            return None
        aliases = self.alias_store.all()
        if not aliases:
            return None
        return self._build_text_item(
            build_owner_alias_note(aliases, bot_name=self._bot_nickname),
            OWNER_NOTE_ITEM_PREFIX,
        )

    # ──── 命令 ────

    @Command(
        "owner_reminder",
        description="主人的叮嘱：下达 / 查看 / 撤销作用于指定聊天流的短期叮嘱",
        pattern=r"(?s)^/(?:叮嘱|influence)(?![0-9A-Za-z_])(?P<rest>.*)$",
    )
    async def handle_owner_reminder(self, **kwargs: Any) -> Tuple[bool, Optional[str], int]:
        """处理 ``/叮嘱``、``/叮嘱列表``、``/叮嘱撤销`` 三种形态。"""

        if not self.config.plugin.enabled:
            return False, None, 1

        context = self._invocation_context(kwargs)
        identity = await self._resolve_owner_identity(context)
        if not self._is_owner_identity(identity):
            self.ctx.logger.info(
                "非主人使用叮嘱命令，已静默忽略: 声明=%s:%s 反查=%s stream=%s",
                context["platform"],
                context["user_id"],
                identity or "<无法确认>",
                context["stream_id"],
            )
            return False, None, 1

        rest = str((kwargs.get("matched_groups") or {}).get("rest") or "")
        request = parse_command(rest, is_alias=self._is_registered_alias)

        if request.action == ACTION_LIST:
            if not request.ok:
                await self._send(context["stream_id"], build_usage_text(request.error))
                return False, None, 1
            return await self._handle_list(context)

        if request.action == ACTION_REVOKE:
            if not request.ok:
                await self._send(context["stream_id"], build_usage_text(request.error))
                return False, None, 1
            return await self._handle_revoke(context, request.argument)

        if not request.ok:
            await self._send(context["stream_id"], build_usage_text(request.error))
            return False, None, 1

        return await self._handle_set(request, context=context, identity=identity)

    async def _handle_set(
        self,
        request: CommandRequest,
        *,
        context: Dict[str, str],
        identity: str,
    ) -> Tuple[bool, Optional[str], int]:
        """下达一条叮嘱：解析目标、落盘、投递提醒并回执。"""

        mode = self.config.dispatch.mode.strip().lower()
        if mode not in DISPATCH_MODES:
            await self._send(
                context["stream_id"],
                build_error_text(
                    f"插件配置 dispatch.mode 只支持 {' / '.join(DISPATCH_MODES)}，当前为 {self.config.dispatch.mode!r}"
                ),
            )
            return False, None, 1

        self._remember_owner_session(context, identity)
        resolved = await self._resolve_target_with_alias(
            request.target_group_id,
            context=context,
        )
        if isinstance(resolved, str):
            await self._send(context["stream_id"], build_error_text(resolved))
            return False, None, 1

        target, alias_name = resolved
        reminder, receipt = await self._apply_directive(
            content=request.content,
            target=target,
            alias_name=alias_name,
            issuer=f"{context['platform']}:{context['user_id']}",
            source_session_id=context["stream_id"],
            mode=mode,
        )
        await self._send(context["stream_id"], receipt)
        return reminder is not None, None, 1

    async def _handle_list(self, context: Dict[str, str]) -> Tuple[bool, Optional[str], int]:
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
        return True, None, 1

    async def _handle_revoke(self, context: Dict[str, str], argument: str) -> Tuple[bool, Optional[str], int]:
        """撤销一条或全部叮嘱。"""

        now = time.time()
        if is_revoke_all(argument):
            removed = self.store.revoke_all(now)
            await self._send(context["stream_id"], build_revoked_text(None, removed))
            return True, None, 1

        removed_one = self.store.revoke(argument, now)
        if removed_one is None:
            await self._send(context["stream_id"], build_missing_reminder_text(argument))
            return False, None, 1
        await self._send(context["stream_id"], build_revoked_text(removed_one, ()))
        return True, None, 1

    # ──── 自然语言工具（只在私聊可见） ────

    @Tool(
        "owner_directive",
        description=(
            "当『主人』明确要求你去影响另一个聊天流的态度或话题时使用，"
            "例如主人说『跟技术群的人说别再吵了』『去群里提一句早点睡』。"
            "它只给那一侧的 planner 留下一层 10 分钟的短期态度影响，不是实时制止。"
            "只有在主人本人要求时才调用；content 只写主人想传达的意思，不要搬运私聊原文。"
        ),
        parameters=[
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="要传达给那个聊天流的意思，写成对她的要求，例如『别继续和某人争执，语气放软』",
                required=True,
            ),
            ToolParameterInfo(
                name="chat",
                param_type=ToolParamType.STRING,
                description="目标聊天流：群号或已登记的群代号；留空表示当前聊天流",
                required=False,
            ),
        ],
        chat_scope="private",
    )
    async def handle_owner_directive(
        self,
        content: str = "",
        chat: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """自然语言下达：由她自己的 planner 调用，落到与 ``/叮嘱`` 完全相同的链路。"""

        context = self._invocation_context(kwargs)
        identity = await self._resolve_owner_identity(context)
        if not self._is_owner_identity(identity):
            self.ctx.logger.info(
                "非主人调用 owner_directive，已拒绝: 声明=%s:%s 反查=%s stream=%s",
                context["platform"],
                context["user_id"],
                identity or "<无法确认>",
                context["stream_id"],
            )
            return {"success": False, "content": build_not_owner_text()}
        self._remember_owner_session(context, identity)

        if not self.config.plugin.enabled or not self.config.tool.enabled:
            return {"success": False, "content": build_tool_disabled_text()}

        mode = self.config.dispatch.mode.strip().lower()
        if mode not in DISPATCH_MODES:
            return {
                "success": False,
                "content": f"插件配置 dispatch.mode 只支持 {' / '.join(DISPATCH_MODES)}，当前为 {self.config.dispatch.mode!r}",
            }

        normalized_content = str(content or "").strip()
        if not normalized_content:
            return {"success": False, "content": build_missing_content_text()}

        resolved = await self._resolve_target_with_alias(chat, context=context)
        if isinstance(resolved, str):
            return {"success": False, "content": resolved}

        target, alias_name = resolved
        reminder, receipt = await self._apply_directive(
            content=normalized_content,
            target=target,
            alias_name=alias_name,
            issuer=f"{context['platform']}:{context['user_id']}",
            source_session_id=context["stream_id"],
            mode=mode,
        )
        return {"success": reminder is not None, "content": receipt}

    @Tool(
        "owner_chat_alias",
        description=(
            "维护『群代号』：主人给某个群起代号时使用，例如主人说『以后把 123456789 叫做技术群』。"
            "action=set 新增或覆盖代号，action=remove 删除代号，action=list 查看现有代号。"
            "只有主人本人可以操作。"
        ),
        parameters=[
            ToolParameterInfo(
                name="action",
                param_type=ToolParamType.STRING,
                description="set / remove / list",
                required=True,
            ),
            ToolParameterInfo(
                name="alias",
                param_type=ToolParamType.STRING,
                description="代号名称，set 与 remove 时必填",
                required=False,
            ),
            ToolParameterInfo(
                name="group",
                param_type=ToolParamType.STRING,
                description="群号，set 时必填",
                required=False,
            ),
        ],
        chat_scope="private",
    )
    async def handle_owner_chat_alias(
        self,
        action: str = "",
        alias: str = "",
        group: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """群代号的增删查。"""

        context = self._invocation_context(kwargs)
        identity = await self._resolve_owner_identity(context)
        if not self._is_owner_identity(identity):
            self.ctx.logger.info(
                "非主人调用 owner_chat_alias，已拒绝: 声明=%s:%s 反查=%s stream=%s",
                context["platform"],
                context["user_id"],
                identity or "<无法确认>",
                context["stream_id"],
            )
            return {"success": False, "content": build_not_owner_text()}
        self._remember_owner_session(context, identity)

        if not self.config.plugin.enabled or not self.config.tool.enabled:
            return {"success": False, "content": build_tool_disabled_text()}

        normalized_action = str(action or "").strip().lower()
        if normalized_action in ALIAS_ACTION_LIST:
            return {"success": True, "content": build_alias_list_text(self.alias_store.all())}
        if normalized_action in ALIAS_ACTION_SET:
            return await self._alias_set(alias=alias, group=group, context=context)
        if normalized_action in ALIAS_ACTION_REMOVE:
            return self._alias_remove(alias)
        return {"success": False, "content": build_unknown_alias_action_text(action)}

    async def _alias_set(self, *, alias: str, group: str, context: Dict[str, str]) -> Dict[str, Any]:
        """登记一个群代号；会先确认这个群确实存在。"""

        if not str(alias or "").strip() or not str(group or "").strip():
            return {"success": False, "content": "设置代号需要同时给出代号名和群号。"}

        platform = context["platform"] or self.config.target.platform
        try:
            target = await resolve_group_target(
                self.ctx.chat,
                platform=platform,
                group_id=str(group).strip(),
                account_id=self.config.target.account_id,
            )
        except TargetResolveError as exc:
            return {"success": False, "content": f"没有把这个群记下来：{exc}"}

        try:
            saved = self.alias_store.set(
                name=alias,
                group_id=target.group_id,
                platform=target.platform,
                group_label=target.label,
                now=time.time(),
            )
        except AliasStoreError as exc:
            return {"success": False, "content": f"代号没保存：{exc}"}
        return {"success": True, "content": build_alias_saved_text(saved)}

    def _alias_remove(self, alias: str) -> Dict[str, Any]:
        """删除一个群代号。"""

        try:
            removed = self.alias_store.remove(alias)
        except AliasStoreError as exc:
            return {"success": False, "content": f"代号没删除：{exc}"}
        if removed is None:
            return {
                "success": False,
                "content": build_alias_missing_text(str(alias or "").strip(), self.alias_store.all()),
            }
        return {"success": True, "content": build_alias_removed_text(removed)}

    # ──── 内部工具 ────

    @staticmethod
    def _invocation_context(kwargs: Dict[str, Any]) -> Dict[str, str]:
        """从命令 / 工具调用参数里取出通用上下文。

        注意：``user_id`` / ``platform`` 只是调用方**声明**的值，工具通道里模型可以
        自己填（宿主合并上下文时"模型给了就不覆盖"），所以它们不能当身份凭据，
        一律交给 :meth:`_resolve_owner_identity` 反查。
        """

        return {
            "stream_id": str(kwargs.get("stream_id") or "").strip(),
            "platform": str(kwargs.get("platform") or "").strip(),
            "user_id": str(kwargs.get("user_id") or "").strip(),
            "group_id": str(kwargs.get("group_id") or "").strip(),
        }

    def _owner_identities(self) -> Set[str]:
        """主人白名单，形如 ``{"qq:100000001"}``。"""

        return {item.strip() for item in self.config.owner.owners if item.strip()}

    def _is_owner_identity(self, identity: str) -> bool:
        """判断一个**已反查出来**的身份是否在主人白名单里。"""

        return bool(identity) and identity in self._owner_identities()

    async def _resolve_owner_identity(self, context: Dict[str, str]) -> str:
        """反查调用者的真实身份，返回 ``platform:user_id``；确认不了返回空串。

        为什么不直接信 ``kwargs["user_id"]``：宿主的插件工具通道
        （``component_query._build_tool_invocation_payload``）在合并上下文时只在
        "模型没给这个键"的情况下才注入真实值，模型只要在工具参数里塞一个
        ``user_id`` 就能冒充主人。所以身份必须从宿主侧的数据反查：

        - **私聊流**：``chat.get_private_streams`` 里 ``session_id`` 命中的那条私聊
          流的 ``user_id`` 就是调用者，模型伪造不了。
        - **群聊流**：SDK 拿不到"这条消息是谁发的"，退化为核对声明的账号确实在这个
          流最近说过话；核对不到一律拒绝（宁可让主人改用 ``/叮嘱``，也不放行
          一个无法确认的身份）。彻底修复需要宿主无条件覆盖身份字段，见 README
          "身份校验"一节。

        能力返回形状不对时直接抛错，不降级成"查不到"。
        """

        stream_id = context["stream_id"]
        platform = context["platform"]
        if not stream_id or not platform:
            return ""

        streams = await self.ctx.chat.get_private_streams(platform)
        if not isinstance(streams, list):
            raise RuntimeError(f"chat.get_private_streams 返回异常，无法校验身份: {streams!r}")
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            if str(stream.get("session_id") or "").strip() != stream_id:
                continue
            user_id = str(stream.get("user_id") or "").strip()
            return f"{platform}:{user_id}" if user_id else ""

        # 不是私聊流：只能核对声明的账号是否真的在这个流里说过话
        claimed = context["user_id"]
        if not claimed:
            return ""
        recent = await self.ctx.message.get_recent(stream_id, limit=IDENTITY_RECENT_SCAN)
        if not isinstance(recent, list):
            raise RuntimeError(f"message.get_recent 返回异常，无法校验身份: {recent!r}")
        for item in recent:
            if not isinstance(item, dict):
                continue
            message_info = item.get("message_info")
            if not isinstance(message_info, dict):
                continue
            user_info = message_info.get("user_info")
            if not isinstance(user_info, dict):
                continue
            if str(user_info.get("user_id") or "").strip() == claimed:
                return f"{platform}:{claimed}"
        return ""

    def _is_registered_alias(self, token: str) -> bool:
        """判断一个 token 是否是已登记的群代号（供命令解析使用）。"""

        return self.alias_store.find(token) is not None

    def _remember_owner_session(self, context: Dict[str, str], identity: str) -> None:
        """把主人的私聊会话记下来，供 hook 注入代号表使用。"""

        if context["group_id"] or not context["stream_id"]:
            return
        if not self._is_owner_identity(identity):
            return
        self._owner_sessions.add(context["stream_id"])

    async def _refresh_bot_nickname(self) -> None:
        """从宿主配置读 bot 昵称，避免把某个具体的名字写死在插件里。

        读失败或类型不对都直接抛：昵称会进注入文本，悄悄退化成「你」只会让主人以为
        插件正常，实际语气全变了。`config.get` 出问题（未授权、字段改名）就该当场看见。
        """

        # SDK 会把 config.get 的返回值拆包：拿到的是配置值本身，不是 {"success": ..., "value": ...}
        value = await self.ctx.call_capability("config.get", key="bot.nickname", default="")
        if not isinstance(value, str):
            # config.get 失败时 SDK 会把 {"success": False} 的 value(None) 交出来；
            # 缺 key 的情况宿主会用 default="" 兜住，所以走到这里就是真的出问题了
            raise TypeError(
                f"读取 bot.nickname 失败（config.get 可能未授权或字段改名），拿到 {value!r}"
            )
        self._bot_nickname = value.strip()

    async def _refresh_owner_sessions(self) -> None:
        """按 owner 白名单尽力解析出主人的私聊会话 ID。

        每次都从空集合重建：白名单删掉某个人之后，他的私聊不该还留着
        「群代号对照 + 工具提示」这条注入。
        """

        self._owner_sessions.clear()
        owners_by_platform: Dict[str, Set[str]] = {}
        for owner in self.config.owner.owners:
            parsed = _split_owner(owner)
            if parsed is None:
                self.ctx.logger.warning("owner.owners 条目格式不正确，已跳过: %r", owner)
                continue
            platform, user_id = parsed
            owners_by_platform.setdefault(platform, set()).add(user_id)

        # 同一平台只查一次私聊流，别为一个 owner 列表反复调同一个能力
        for platform, user_ids in owners_by_platform.items():
            streams = await self.ctx.chat.get_private_streams(platform)
            if not isinstance(streams, list):
                # 形状不对说明能力契约变了/未授权：代号对照表会整块消失，必须当场暴露
                raise RuntimeError(f"chat.get_private_streams 返回异常，无法刷新主人私聊会话: {streams!r}")
            for stream in streams:
                if not isinstance(stream, dict):
                    continue
                if str(stream.get("user_id") or "").strip() not in user_ids:
                    continue
                session_id = str(stream.get("session_id") or "").strip()
                if session_id:
                    self._owner_sessions.add(session_id)

    async def _resolve_target_with_alias(
        self,
        token: str,
        *,
        context: Dict[str, str],
    ) -> Union[Tuple[ResolvedTarget, str], str]:
        """把 群号 / 代号 / 空 解析成目标聊天流。

        Returns:
            tuple[ResolvedTarget, str] | str: 成功时返回 (目标, 用到的代号)，失败时返回错误文本。
        """

        normalized_token = str(token or "").strip()
        alias_name = ""
        platform = context["platform"] or self.config.target.platform

        if normalized_token and not (normalized_token.isascii() and normalized_token.isdigit()):
            alias = self.alias_store.find(normalized_token)
            if alias is None:
                return build_unknown_alias_text(normalized_token, self.alias_store.all())
            normalized_token = alias.group_id
            platform = alias.platform or platform
            alias_name = alias.name

        target = await self._resolve_target(
            normalized_token,
            context=context,
            platform=platform,
        )
        if isinstance(target, str):
            return target
        return target, alias_name

    async def _resolve_target(
        self,
        target_group_id: str,
        *,
        context: Dict[str, str],
        platform: str,
    ) -> Union[ResolvedTarget, str]:
        """解析目标聊天流；失败时返回错误文本。"""

        group_id = str(target_group_id or "").strip() or context["group_id"]
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

    async def _apply_directive(
        self,
        *,
        content: str,
        target: ResolvedTarget,
        alias_name: str,
        issuer: str,
        source_session_id: str,
        mode: str,
    ) -> Tuple[Optional[Reminder], str]:
        """落盘 + 按 dispatch.mode 投递；返回 (提醒, 给用户或模型看的文本)。

        命令与工具都走这里，保证两条通道的行为与落盘结构完全一致。
        """

        max_content_chars = self.config.reminder.max_content_chars
        truncated = len(content) > max_content_chars
        try:
            reminder, evicted = self.store.create(
                target_session_id=target.session_id,
                target_label=target.label,
                content=content[:max_content_chars],
                issuer=issuer,
                source_session_id=source_session_id,
                ttl_seconds=float(self.config.reminder.default_ttl_seconds),
                now=time.time(),
            )
        except ReminderStoreError as exc:
            # 只兜「状态文件写不进去」这一种；其它异常（编程错误、契约漂移）照常往上抛，
            # 不然它们会被伪装成"提醒投递出错"这种看起来像业务规则的提示
            self.ctx.logger.error("保存叮嘱失败: %s", exc, exc_info=True)
            return None, build_error_text(f"叮嘱没能保存：{exc}")

        try:
            dispatch_note = await self._dispatch_reminder(reminder, mode=mode)
        except Exception as exc:  # 投递出错也要让主人知道叮嘱已经生效，别以为命令没成功
            self.ctx.logger.error("朝目标流投递提醒异常: %s", exc, exc_info=True)
            dispatch_note = f"⚠️ 但提醒投递出错（{exc}）：叮嘱已保存，会在目标流下次自然触发时生效。"

        receipt = build_created_receipt(
            reminder,
            ttl_seconds=self.config.reminder.default_ttl_seconds,
            truncated=truncated,
            evicted=evicted,
            dispatch_note=dispatch_note,
            alias_name=alias_name,
        )
        return reminder, receipt

    async def _dispatch_reminder(self, reminder: Reminder, *, mode: str) -> str:
        """按 ``dispatch.mode`` 决定是否朝目标流投一次提醒。

        Returns:
            str: 追加到回执里的说明文本。
        """

        if mode == "silent":
            return "目标流没有被惊动，她会在下次自然触发时看到这条叮嘱。"

        # 模板写坏（多了个未知占位符）就在这里抛 KeyError，由调用方转成「提醒投递出错」的回执：
        # 宁可让主人看到一次明确的配置错误，也不要静默发一条没有叮嘱内容的 intent 过去。
        intent = self.config.dispatch.intent_template.format(内容=reminder.content)
        # 先记「投递过」再真正投递：proactive.trigger 只是入队并唤醒目标流，目标流那一轮的
        # planner→reply 完全可能在这次 await 返回之前就跑完；那时 consume_dispatched 因为
        # dispatched_at 还是 0 而认不出这是「兑现」，叮嘱会一直留到 TTL，期间每轮 planner
        # 继续被注入「主人刚特意交代过」——已经照做了却还在施压。
        self.store.mark_dispatched(reminder.id, time.time())
        try:
            result = await self.ctx.maisaka.proactive.trigger(
                stream_id=reminder.target_session_id,
                intent=intent,
                reason="主人下达了叮嘱",
            )
        except Exception:
            # 投递本身炸了：撤销记账，退回纯 TTL 语义，别让这条叮嘱永远等不到「兑现」
            self.store.clear_dispatched(reminder.id)
            raise
        if not isinstance(result, dict) or not result.get("success"):
            error = result.get("error") if isinstance(result, dict) else repr(result)
            self.ctx.logger.error("朝目标流投递提醒失败: %s", error)
            self.store.clear_dispatched(reminder.id)
            return f"⚠️ 但提醒没投出去（{error}）：叮嘱已保存，会在目标流下次自然触发时生效。"

        self.ctx.logger.info(
            "已朝目标流投递一次提醒: session_id=%s task_id=%s",
            reminder.target_session_id,
            result.get("task_id"),
        )
        return "已提醒她自己去判断要不要现在处理。"

    async def _send(self, stream_id: str, text: str) -> None:
        """发送回执。

        回执是给主人的界面反馈：宿主默认 `sync_to_maisaka_history=False`，所以它**不进**
        她的 planner 上下文（但默认 `storage_message=True`，会入库并在聊天记录里可见）。
        """

        if not stream_id:
            self.ctx.logger.warning("回执缺少 stream_id，已放弃发送: %s", text)
            return
        # 必须带 return_details：默认形态下 SDK 会把结果塌成 bool(success)，
        # 真实的 sent 字段被丢掉，于是"发失败"和"发成功"看起来一模一样
        result = await self.ctx.send.text(text, stream_id, return_details=True)
        sent = bool(result.get("sent")) if isinstance(result, dict) else bool(result)
        if not sent:
            self.ctx.logger.error("回执发送失败: stream_id=%s text=%s result=%r", stream_id, text, result)


def _split_owner(entry: str) -> Optional[Tuple[str, str]]:
    """把 `platform:user_id` 拆开；结构不对返回 None。

    只认英文冒号：`qq：123456789`（全角冒号）这类写法永远匹配不上白名单，
    必须在加载时就把它点出来，否则用户只会看到「填了却没权限」。
    """

    platform, separator, user_id = str(entry or "").partition(":")
    platform = platform.strip()
    user_id = user_id.strip()
    if not separator or not platform or not user_id:
        return None
    return platform, user_id


def create_plugin() -> OwnerReminderPlugin:
    """创建插件实例。"""

    return OwnerReminderPlugin()

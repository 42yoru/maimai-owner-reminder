"""主人的叮嘱：目标聊天流解析。

只通过 SDK 的 ``chat.get_group_streams`` 解析群号对应的真实 session_id：
解析不到、或同一群号命中多条时直接报错，绝不自行计算 hash，也不猜。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from maibot_sdk.capabilities.chat import ChatCapability


class TargetResolveError(RuntimeError):
    """目标聊天流无法唯一解析。"""


@dataclass(frozen=True)
class ResolvedTarget:
    """解析后的目标聊天流。"""

    session_id: str
    label: str
    group_id: str
    account_id: str
    platform: str


async def resolve_group_target(
    chat: ChatCapability,
    *,
    platform: str,
    group_id: str,
    account_id: str = "",
) -> ResolvedTarget:
    """把群号解析成已存在的真实聊天流。

    Args:
        chat: SDK 聊天流能力代理。
        platform: 平台标识，例如 ``qq``。
        group_id: 目标群号。
        account_id: 命中多条聊天流时用于消歧的账号 ID；留空表示要求唯一命中。

    Returns:
        ResolvedTarget: 目标聊天流。

    Raises:
        TargetResolveError: 群号为空、读取失败、没有命中、命中多条或多条消歧失败。
    """

    normalized_group_id = str(group_id or "").strip()
    if not normalized_group_id:
        raise TargetResolveError("群号不能为空")

    streams = _normalize_streams(await chat.get_group_streams(platform))
    matches = [stream for stream in streams if _group_id_of(stream) == normalized_group_id]
    if not matches:
        raise TargetResolveError(
            f"没有找到群 {normalized_group_id} 对应的聊天流（平台 {platform}）"
            "：请确认这个群已接入且已产生过消息。"
        )

    normalized_account_id = str(account_id or "").strip()
    if normalized_account_id:
        # 配置了账号就一律按它过滤：哪怕只命中一条也要确认账号一致，
        # 否则可能打到退役账号留下的旧聊天流上
        filtered = [stream for stream in matches if _account_id_of(stream) == normalized_account_id]
        if not filtered:
            accounts = sorted({_account_id_of(stream) or "<空>" for stream in matches})
            raise TargetResolveError(
                f"群 {normalized_group_id} 在账号 {normalized_account_id} 下没有聊天流"
                f"（当前命中的账号：{'、'.join(accounts)}）"
            )
        matches = filtered

    if len(matches) > 1:
        accounts = sorted({_account_id_of(stream) or "<空>" for stream in matches})
        raise TargetResolveError(
            f"群 {normalized_group_id} 命中 {len(matches)} 条聊天流（账号：{'、'.join(accounts)}）"
            "：请在插件配置 target.account_id 里指定要作用的账号。"
        )

    chosen = matches[0]
    session_id = str(chosen.get("session_id") or "").strip()
    if not session_id:
        raise TargetResolveError(f"群 {normalized_group_id} 的聊天流缺少 session_id")
    label = str(chosen.get("group_name") or "").strip() or f"群 {normalized_group_id}"
    return ResolvedTarget(
        session_id=session_id,
        label=label,
        group_id=normalized_group_id,
        account_id=_account_id_of(chosen),
        platform=platform,
    )


def _normalize_streams(raw_streams: Any) -> List[Dict[str, Any]]:
    """校验能力返回；失败时带着原始信息报错。"""

    if isinstance(raw_streams, list):
        invalid_items = [item for item in raw_streams if not isinstance(item, dict)]
        if invalid_items:
            raise TargetResolveError(f"群聊流列表包含非对象条目：{invalid_items[0]!r}")
        return raw_streams
    if isinstance(raw_streams, dict):
        raise TargetResolveError(f"读取群聊流失败：{raw_streams.get('error') or raw_streams}")
    raise TargetResolveError(f"读取群聊流返回了意外类型：{type(raw_streams).__name__}")


def _group_id_of(stream: Dict[str, Any]) -> str:
    """取出聊天流里的群号。"""

    return str(stream.get("group_id") or "").strip()


def _account_id_of(stream: Dict[str, Any]) -> str:
    """取出聊天流里的账号 ID。"""

    return str(stream.get("account_id") or "").strip()

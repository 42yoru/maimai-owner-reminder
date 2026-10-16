"""主人的叮嘱：注入文案与对外回执文案。

所有面向用户的文本都集中在本模块，保持简体中文。
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from .reminder_store import Reminder, ReminderStage

INJECT_ITEM_ID_PREFIX = "owner_reminder_"

COMMAND_PREFIX = "/叮嘱"
COMMAND_ALIAS_HINT = "（别名 /influence）"

# 注入文案按档位区分：强档明确、弱档只留倾向，都不允许暴露叮嘱来源
STRONG_TEMPLATE = (
    "【内部参考·主人叮嘱】主人刚特意交代过：{content}。"
    "请在当前聊天里自然体现出这个态度，不要提到这条叮嘱的存在，也不要复述主人的原话。"
)
WEAK_TEMPLATE = (
    "【内部参考·主人叮嘱】主人此前提过：{content}。"
    "如果与当前话题相关，请保持这个态度倾向；不必刻意提起。"
)


def build_injection_text(reminder: Reminder, stage: ReminderStage) -> str:
    """按档位生成注入文本。

    Args:
        reminder: 目标叮嘱。
        stage: 当前档位，只能是 ``strong`` 或 ``weak``。

    Returns:
        str: 注入到目标流 planner 上下文的文本。

    Raises:
        ValueError: 传入 ``expired`` 档位时抛出（到期的叮嘱不应被注入）。
    """

    if stage == "strong":
        return STRONG_TEMPLATE.format(content=reminder.content)
    if stage == "weak":
        return WEAK_TEMPLATE.format(content=reminder.content)
    raise ValueError(f"到期的叮嘱不应生成注入文本: {reminder.id}")


def format_remaining(seconds: float) -> str:
    """把剩余秒数格式化为 mm:ss。"""

    total_seconds = max(0, int(seconds))
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


def format_stage(stage: ReminderStage) -> str:
    """把档位格式化为中文标签。"""

    if stage == "strong":
        return "强"
    if stage == "weak":
        return "弱"
    return "已到期"


def build_usage_text(reason: str = "") -> str:
    """生成用法说明文本。"""

    lines: List[str] = []
    if reason:
        lines.append(f"❌ {reason}")
    lines.extend(
        [
            f"用法{COMMAND_ALIAS_HINT}：",
            f"  {COMMAND_PREFIX} <群号> <内容>   作用于指定群",
            f"  {COMMAND_PREFIX} @here <内容>    作用于当前聊天流",
            f"  {COMMAND_PREFIX}列表             查看生效中的叮嘱",
            f"  {COMMAND_PREFIX}撤销 <编号|all>  撤销一条或全部",
        ]
    )
    return "\n".join(lines)


def build_error_text(message: str) -> str:
    """生成错误回执文本。"""

    return f"❌ {message}"


def build_created_receipt(
    reminder: Reminder,
    *,
    ttl_seconds: float,
    truncated: bool,
    evicted: Sequence[Reminder],
    dispatch_note: str,
) -> str:
    """生成下达成功的回执。"""

    lines = [
        f"✅ 已记下叮嘱 #{reminder.id}（作用于「{reminder.target_label}」）",
        f"   生效 {ttl_seconds / 60:.0f} 分钟，之后自动淡出。查看：{COMMAND_PREFIX}列表",
    ]
    if truncated:
        lines.append("   ⚠️ 内容超过配置上限，已截断保存。")
    if evicted:
        evicted_ids = "、".join(f"#{item.id}" for item in evicted)
        lines.append(f"   ♻️ 同一聊天流超出上限，已替换掉最旧的：{evicted_ids}")
    if dispatch_note:
        lines.append(f"   {dispatch_note}")
    return "\n".join(lines)


def build_list_text(reminders: Sequence[Reminder], *, now: float, strong_ratio: float) -> str:
    """生成生效中叮嘱的列表文本。"""

    if not reminders:
        return f"📋 当前没有生效中的叮嘱。用 {COMMAND_PREFIX} <群号> <内容> 下达一条。"

    lines = [f"📋 生效中的叮嘱（{len(reminders)} 条）"]
    for item in reminders:
        stage = item.stage(now, strong_ratio)
        lines.append(
            f"  #{item.id} 【{format_stage(stage)}】「{item.target_label}」"
            f"  剩余 {format_remaining(item.remaining_seconds(now))}  「{item.content}」"
        )
    return "\n".join(lines)


def build_revoked_text(reminder: Optional[Reminder], removed_all: Sequence[Reminder]) -> str:
    """生成撤销结果文本。"""

    if reminder is not None:
        return f"✅ 已撤销叮嘱 #{reminder.id}（「{reminder.target_label}」）。"
    if removed_all:
        ids = "、".join(f"#{item.id}" for item in removed_all)
        return f"✅ 已撤销全部 {len(removed_all)} 条叮嘱：{ids}。"
    return "📋 当前没有生效中的叮嘱，无需撤销。"


def build_missing_reminder_text(reminder_id: str) -> str:
    """生成找不到指定编号的提示。"""

    return f"❌ 没有找到编号为 {reminder_id} 的叮嘱（可能已到期或被替换）。用 {COMMAND_PREFIX}列表 查看现有条目。"

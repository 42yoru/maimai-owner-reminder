"""主人的叮嘱：命令参数解析。

解析结果要么明确、要么带上错误原因，不做猜测。
"""

from __future__ import annotations

from dataclasses import dataclass

HERE_MARKER = "@here"
LIST_WORDS = ("列表", "list")
REVOKE_WORDS = ("撤销", "revoke")
ALL_MARKERS = ("all", "全部")

ACTION_SET = "set"
ACTION_LIST = "list"
ACTION_REVOKE = "revoke"


@dataclass(frozen=True)
class CommandRequest:
    """解析后的命令请求。"""

    action: str = ACTION_SET
    target_group_id: str = ""
    content: str = ""
    argument: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        """请求是否可用。"""

        return not self.error


def parse_command(rest: str) -> CommandRequest:
    """解析命令词之后的整段原文。

    规则：

    - 首个 token 是 ``列表`` / ``list`` → 查看当前生效的叮嘱；
    - 首个 token 是 ``撤销`` / ``revoke`` → 撤销，后面的 token 是编号或 all；
    - 首个 token 是纯数字 → 它是目标群号，其后是叮嘱内容；
    - 首个 token 是 ``@here`` → 目标是当前聊天流，其后是叮嘱内容；
    - 其他情况 → 目标是当前聊天流，整段都是叮嘱内容。

    Args:
        rest: 命令词之后、已去掉首尾空白的原文。

    Returns:
        CommandRequest: 动作与参数，或错误原因。
    """

    text = str(rest or "").strip()
    if not text:
        return CommandRequest(error="缺少叮嘱内容")

    parts = text.split(maxsplit=1)
    head = parts[0]
    tail = parts[1].strip() if len(parts) > 1 else ""

    if head in LIST_WORDS:
        if tail:
            return CommandRequest(action=ACTION_LIST, error=f"{head} 后面不需要参数")
        return CommandRequest(action=ACTION_LIST)

    if head in REVOKE_WORDS:
        if not tail:
            return CommandRequest(action=ACTION_REVOKE, error="撤销需要给出编号或 all")
        if len(tail.split()) != 1:
            return CommandRequest(action=ACTION_REVOKE, error="撤销参数只能是一个编号或 all")
        return CommandRequest(action=ACTION_REVOKE, argument=tail)

    if head == HERE_MARKER:
        if not tail:
            return CommandRequest(error=f"{HERE_MARKER} 后面缺少叮嘱内容")
        return CommandRequest(content=tail)

    if head.isascii() and head.isdigit():
        if not tail:
            return CommandRequest(error=f"群号 {head} 后面缺少叮嘱内容")
        return CommandRequest(target_group_id=head, content=tail)

    return CommandRequest(content=text)


def is_revoke_all(argument: str) -> bool:
    """判断撤销参数是否表示"全部"。"""

    return str(argument or "").strip().lower() in ALL_MARKERS

"""主人的叮嘱：群代号（别名）表。

与 reminders.json 同规格：原子写、损坏即报错，不做静默兜底。
本模块不依赖插件上下文，只接受 data_dir，便于单元测试。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import json
import os

STORE_FILE_NAME = "aliases.json"
STORE_VERSION = 1
MAX_ALIAS_CHARS = 20


class AliasStoreError(RuntimeError):
    """代号表损坏、不可读写，或代号本身不合法。"""


@dataclass(frozen=True)
class ChatAlias:
    """一个群代号。"""

    name: str
    group_id: str
    platform: str
    group_label: str
    updated_at: float


def normalize_alias_name(name: str) -> str:
    """去掉首尾空白。"""

    return str(name or "").strip()


def validate_alias_name(name: str) -> str:
    """校验代号；合法时返回空字符串，否则返回给用户看的原因。"""

    if not name:
        return "代号不能为空"
    if len(name) > MAX_ALIAS_CHARS:
        return f"代号不能超过 {MAX_ALIAS_CHARS} 个字符"
    if any(character.isspace() for character in name):
        return "代号里不能有空格或换行"
    if name.isascii() and name.isdigit():
        return "代号不能是纯数字，否则会和群号混淆"
    return ""


class AliasStore:
    """群代号表；写入使用临时文件 + 原子替换。"""

    def __init__(self, data_dir: Path) -> None:
        """初始化存储。

        Args:
            data_dir: 插件独立数据目录。
        """

        self._data_dir = Path(data_dir)
        self._aliases: Optional[List[ChatAlias]] = None

    @property
    def path(self) -> Path:
        """状态文件路径。"""

        return self._data_dir / STORE_FILE_NAME

    # ──── 读写 ────

    def _ensure_loaded(self) -> List[ChatAlias]:
        """惰性读取代号表。"""

        if self._aliases is None:
            self._aliases = self._read_file()
        return self._aliases

    def _read_file(self) -> List[ChatAlias]:
        """读取并严格校验代号表。"""

        if not self.path.exists():
            return []
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AliasStoreError(f"读取群代号表失败: {self.path}: {exc}") from exc
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise AliasStoreError(f"群代号表不是合法 JSON: {self.path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise AliasStoreError(f"群代号表顶层必须是对象: {self.path}")
        if payload.get("version") != STORE_VERSION:
            raise AliasStoreError(
                f"群代号表版本不受支持: {payload.get('version')!r}（当前 {STORE_VERSION}）"
            )
        raw_aliases = payload.get("aliases")
        if not isinstance(raw_aliases, list):
            raise AliasStoreError(f"群代号表缺少 aliases 列表: {self.path}")
        return [self._decode_alias(item) for item in raw_aliases]

    @staticmethod
    def _decode_alias(raw: Any) -> ChatAlias:
        """把一条 JSON 记录还原成 ChatAlias。"""

        if not isinstance(raw, dict):
            raise AliasStoreError(f"群代号条目必须是对象: {raw!r}")
        for key in ("name", "group_id"):
            value = raw.get(key)
            if not isinstance(value, str) or not value.strip():
                raise AliasStoreError(f"群代号条目缺少非空字符串字段 {key}: {raw!r}")
        try:
            updated_at = float(raw.get("updated_at", 0.0))
        except (TypeError, ValueError) as exc:
            raise AliasStoreError(f"群代号条目时间字段不合法: {raw!r}") from exc
        return ChatAlias(
            name=raw["name"].strip(),
            group_id=raw["group_id"].strip(),
            platform=str(raw.get("platform") or "").strip(),
            group_label=str(raw.get("group_label") or "").strip(),
            updated_at=updated_at,
        )

    def _write_file(self, aliases: List[ChatAlias]) -> None:
        """原子写入代号表。"""

        self._data_dir.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, Any] = {
            "version": STORE_VERSION,
            "aliases": [asdict(item) for item in aliases],
        }
        temp_path = self.path.parent / (self.path.name + ".tmp")
        try:
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
        except OSError as exc:
            raise AliasStoreError(f"写入群代号表失败: {self.path}: {exc}") from exc

    # ──── 查询与维护 ────

    def all(self) -> List[ChatAlias]:
        """返回全部代号，按代号排序。"""

        return sorted(self._ensure_loaded(), key=lambda item: item.name)

    def find(self, name: str) -> Optional[ChatAlias]:
        """按代号查找，忽略大小写与首尾空白。"""

        normalized_name = normalize_alias_name(name)
        if not normalized_name:
            return None
        folded = normalized_name.casefold()
        for item in self.all():
            if item.name.casefold() == folded:
                return item
        return None

    def set(
        self,
        *,
        name: str,
        group_id: str,
        platform: str,
        group_label: str,
        now: float,
    ) -> ChatAlias:
        """新增或覆盖一个代号。

        Raises:
            AliasStoreError: 代号不合法、群号为空或写入失败。
        """

        normalized_name = normalize_alias_name(name)
        error = validate_alias_name(normalized_name)
        if error:
            raise AliasStoreError(error)
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            raise AliasStoreError("群号不能为空")

        alias = ChatAlias(
            name=normalized_name,
            group_id=normalized_group_id,
            platform=str(platform or "").strip(),
            group_label=str(group_label or "").strip(),
            updated_at=float(now),
        )
        folded = normalized_name.casefold()
        kept = [item for item in self._ensure_loaded() if item.name.casefold() != folded]
        self._commit([*kept, alias])
        return alias

    def _commit(self, candidate: List[ChatAlias]) -> None:
        """先原子写盘，成功后才切换内存状态（口径与 ReminderStore._commit 一致）。"""

        self._write_file(candidate)
        self._aliases = candidate

    def remove(self, name: str) -> Optional[ChatAlias]:
        """删除一个代号；不存在时返回 None。"""

        target = self.find(name)
        if target is None:
            return None
        folded = target.name.casefold()
        self._commit([item for item in self._ensure_loaded() if item.name.casefold() != folded])
        return target

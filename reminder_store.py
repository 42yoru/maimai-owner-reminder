"""主人的叮嘱：状态存储与时效衰减。

本模块刻意不依赖插件上下文，只接受 data_dir 与策略参数，方便单元测试。
状态文件损坏时直接抛错，不做静默兜底。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

import json
import os
import uuid

STORE_FILE_NAME = "reminders.json"
STORE_VERSION = 1

ReminderStage = Literal["strong", "weak", "expired"]


class ReminderStoreError(RuntimeError):
    """叮嘱状态文件缺失必要字段、损坏或不可读写。"""


@dataclass(frozen=True)
class Reminder:
    """一条叮嘱。"""

    id: str
    target_session_id: str
    target_label: str
    content: str
    issuer: str
    source_session_id: str
    created_at: float
    ttl_seconds: float
    # 朝目标流投递过一次提醒的时刻；0 表示没有投递过（silent 模式，或投递失败）
    dispatched_at: float = 0.0

    @property
    def dispatched(self) -> bool:
        """这条叮嘱是否已经朝目标流投递过提醒。"""

        return self.dispatched_at > 0.0

    def stage(self, now: float, strong_ratio: float) -> ReminderStage:
        """按墙上时钟判断这条叮嘱当前处在哪一档。

        Args:
            now: 当前 Unix 时间戳。
            strong_ratio: 强档占生效时长的比例。

        Returns:
            ReminderStage: ``strong`` / ``weak`` / ``expired``。
        """

        elapsed = now - self.created_at
        if elapsed >= self.ttl_seconds:
            return "expired"
        if elapsed < self.ttl_seconds * strong_ratio:
            return "strong"
        return "weak"

    def remaining_seconds(self, now: float) -> float:
        """返回剩余生效秒数，最小为 0。"""

        return max(0.0, self.ttl_seconds - (now - self.created_at))


class ReminderStore:
    """叮嘱状态文件；写入使用临时文件 + 原子替换，避免半份状态。"""

    def __init__(
        self,
        data_dir: Path,
        *,
        max_per_target: int,
        strong_ratio: float,
    ) -> None:
        """初始化存储。

        Args:
            data_dir: 插件独立数据目录。
            max_per_target: 同一目标流最多同时生效几条。
            strong_ratio: 强档占生效时长的比例。
        """

        self._data_dir = Path(data_dir)
        self._max_per_target = max_per_target
        self._strong_ratio = strong_ratio
        self._reminders: Optional[List[Reminder]] = None

    @property
    def path(self) -> Path:
        """状态文件路径。"""

        return self._data_dir / STORE_FILE_NAME

    # ──── 读写 ────

    def _ensure_loaded(self) -> List[Reminder]:
        """惰性读取状态文件。"""

        if self._reminders is None:
            self._reminders = self._read_file()
        return self._reminders

    def _read_file(self) -> List[Reminder]:
        """读取并严格校验状态文件。"""

        if not self.path.exists():
            return []
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReminderStoreError(f"读取叮嘱状态文件失败: {self.path}: {exc}") from exc
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ReminderStoreError(f"叮嘱状态文件不是合法 JSON: {self.path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ReminderStoreError(f"叮嘱状态文件顶层必须是对象: {self.path}")
        if payload.get("version") != STORE_VERSION:
            raise ReminderStoreError(
                f"叮嘱状态文件版本不受支持: {payload.get('version')!r}（当前 {STORE_VERSION}）"
            )
        raw_reminders = payload.get("reminders")
        if not isinstance(raw_reminders, list):
            raise ReminderStoreError(f"叮嘱状态文件缺少 reminders 列表: {self.path}")
        return [self._decode_reminder(item) for item in raw_reminders]

    @staticmethod
    def _decode_reminder(raw: Any) -> Reminder:
        """把一条 JSON 记录还原成 Reminder。"""

        if not isinstance(raw, dict):
            raise ReminderStoreError(f"叮嘱条目必须是对象: {raw!r}")
        for key in ("id", "target_session_id", "content", "issuer", "source_session_id"):
            value = raw.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ReminderStoreError(f"叮嘱条目缺少非空字符串字段 {key}: {raw!r}")
        try:
            created_at = float(raw["created_at"])
            ttl_seconds = float(raw["ttl_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReminderStoreError(f"叮嘱条目时间字段不合法: {raw!r}") from exc
        try:
            dispatched_at = float(raw.get("dispatched_at") or 0.0)
        except (TypeError, ValueError) as exc:
            raise ReminderStoreError(f"叮嘱条目投递时间字段不合法: {raw!r}") from exc
        return Reminder(
            id=raw["id"].strip(),
            target_session_id=raw["target_session_id"].strip(),
            target_label=str(raw.get("target_label") or "").strip(),
            content=raw["content"],
            issuer=raw["issuer"].strip(),
            source_session_id=raw["source_session_id"].strip(),
            created_at=created_at,
            ttl_seconds=ttl_seconds,
            dispatched_at=dispatched_at,
        )

    def _write_file(self, reminders: List[Reminder]) -> None:
        """原子写入状态文件。"""

        self._data_dir.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, Any] = {
            "version": STORE_VERSION,
            "reminders": [asdict(item) for item in reminders],
        }
        temp_path = self.path.parent / (self.path.name + ".tmp")
        try:
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
        except OSError as exc:
            raise ReminderStoreError(f"写入叮嘱状态文件失败: {self.path}: {exc}") from exc

    def _commit(self, candidate: List[Reminder]) -> None:
        """先原子写盘，成功后才切换内存状态。

        写失败时内存必须保持旧值：回执说「没保存」就真的要没保存，否则磁盘满 / 权限错误之后，
        那条叮嘱仍会被注入目标流，下一次任意成功写盘还会把它持久化。
        """

        self._write_file(candidate)
        self._reminders = candidate

    # ──── 查询 ────

    def active_for(self, session_id: str, now: float) -> List[Reminder]:
        """返回某个聊天流当前生效的叮嘱，按创建时间升序。

        顺带清掉过期条目：注入路径每轮都会走到这里，过期记录留久了只会让文件越滚越大。
        """

        self.purge_expired(now)
        return sorted(
            [item for item in self._ensure_loaded() if item.target_session_id == session_id],
            key=lambda item: item.created_at,
        )

    def active_all(self, now: float) -> List[Reminder]:
        """返回全部生效中的叮嘱，按创建时间升序。清理口径与 active_for 一致。"""

        self.purge_expired(now)
        return sorted(self._ensure_loaded(), key=lambda item: item.created_at)

    def purge_expired(self, now: float) -> List[Reminder]:
        """清除已到期条目，返回被清除的条目。"""

        reminders = self._ensure_loaded()
        expired = [item for item in reminders if item.stage(now, self._strong_ratio) == "expired"]
        if expired:
            expired_ids = {item.id for item in expired}
            self._commit([item for item in reminders if item.id not in expired_ids])
        return expired

    # ──── 写入 ────

    def create(
        self,
        *,
        target_session_id: str,
        target_label: str,
        content: str,
        issuer: str,
        source_session_id: str,
        ttl_seconds: float,
        now: float,
    ) -> Tuple[Reminder, List[Reminder]]:
        """新增一条叮嘱。

        Returns:
            Tuple[Reminder, List[Reminder]]: 新叮嘱，以及因为超出同目标上限而被替换掉的旧叮嘱。
        """

        self.purge_expired(now)
        reminders = self._ensure_loaded()
        reminder = Reminder(
            id=self._new_id({item.id for item in reminders}),
            target_session_id=target_session_id,
            target_label=target_label,
            content=content,
            issuer=issuer,
            source_session_id=source_session_id,
            created_at=now,
            ttl_seconds=float(ttl_seconds),
        )

        # 同一目标流超过上限时，按最旧替换，避免 prompt 里堆积过期态度
        same_target = sorted(
            (item for item in reminders if item.target_session_id == target_session_id),
            key=lambda item: item.created_at,
        )
        overflow = max(0, len(same_target) - self._max_per_target + 1)
        evicted = same_target[:overflow]
        evicted_ids = {item.id for item in evicted}

        remaining = [item for item in reminders if item.id not in evicted_ids]
        remaining.append(reminder)
        self._commit(remaining)
        return reminder, evicted

    # ──── 投递与消费 ────

    def mark_dispatched(self, reminder_id: str, now: float) -> Optional[Reminder]:
        """记录这条叮嘱已经朝目标流投递过一次提醒。

        只有投递过的条目才会被"兑现即消费"清掉：silent 模式没有投递，
        也就没有"因为叮嘱触发一次发言"这回事，必须继续按 TTL 兜底。

        Returns:
            Optional[Reminder]: 更新后的条目；找不到时返回 None。
        """

        self.purge_expired(now)
        for item in self._ensure_loaded():
            if item.id != reminder_id:
                continue
            updated = replace(item, dispatched_at=now)
            self._commit([updated if candidate.id == item.id else candidate for candidate in self._ensure_loaded()])
            return updated
        return None

    def clear_dispatched(self, reminder_id: str) -> None:
        """撤销「已投递」标记，让条目回到纯 TTL 语义。

        投递失败时用它回滚：`proactive.trigger` 没成功就说明目标流根本没被惊动，
        这条叮嘱不该被后续任何一次开口"兑现"掉。
        """

        for item in self._ensure_loaded():
            if item.id != reminder_id or not item.dispatched:
                continue
            self._commit(
                [
                    replace(candidate, dispatched_at=0.0) if candidate.id == item.id else candidate
                    for candidate in self._ensure_loaded()
                ]
            )
            return

    def consume_dispatched(self, session_id: str, now: float) -> List[Reminder]:
        """消费掉目标流里已经投递过的叮嘱：她已经在那个聊天流说过话了。

        先清过期：已经淡出的条目不该被记成「兑现」，否则日志会把自然过期说成消费。

        只清"投递过"的条目；没有投递过（silent / 投递失败）的条目保持原样，
        继续由 TTL 决定寿命。

        Returns:
            List[Reminder]: 被消费掉的条目；没有可消费条目时不写盘。
        """

        self.purge_expired(now)
        reminders = self._ensure_loaded()
        hits = [item for item in reminders if item.target_session_id == session_id and item.dispatched]
        if not hits:
            return []
        consumed_ids = {item.id for item in hits}
        self._commit([item for item in reminders if item.id not in consumed_ids])
        return hits

    def revoke(self, reminder_id: str, now: float) -> Optional[Reminder]:
        """按编号撤销一条叮嘱；找不到时返回 None。"""

        self.purge_expired(now)
        normalized_id = str(reminder_id or "").strip().lstrip("#").lower()
        for item in self._ensure_loaded():
            if item.id != normalized_id:
                continue
            self._commit([candidate for candidate in self._ensure_loaded() if candidate.id != item.id])
            return item
        return None

    def revoke_all(self, now: float) -> List[Reminder]:
        """撤销全部生效中的叮嘱，返回被撤销的条目。"""

        self.purge_expired(now)
        removed = list(self._ensure_loaded())
        if removed:
            self._commit([])
        return removed

    @staticmethod
    def _new_id(existing_ids: Set[str]) -> str:
        """生成一个不与现有条目冲突的短编号。"""

        while True:
            candidate = uuid.uuid4().hex[:4]
            if candidate not in existing_ids:
                return candidate

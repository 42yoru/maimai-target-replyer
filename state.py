"""插件状态：消息去重、每群冷却、全局小时额度。

状态落在插件数据目录的 `state.json`，用「临时文件 + os.replace」原子写，
避免进程被杀时留下半截 JSON。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import json
import os
import time

PROCESSED_TTL_SECONDS = 24 * 3600
"""已处理消息 ID 的保留时长。"""

HOURLY_WINDOW_SECONDS = 3600
"""小时额度统计窗口。"""


class StateError(RuntimeError):
    """状态文件读写失败。"""


class StateStore:
    """去重 / 冷却 / 额度的持久化状态。"""

    def __init__(self, data_dir: Path) -> None:
        """初始化状态存储。

        Args:
            data_dir: 插件数据目录（由宿主分配）。
        """

        self._path = Path(data_dir) / "state.json"
        self._processed: Dict[str, float] = {}
        self._last_trigger: Dict[str, float] = {}
        self._trigger_times: List[float] = []

    @property
    def path(self) -> Path:
        """返回状态文件路径。"""

        return self._path

    def load(self) -> None:
        """从磁盘读取状态；文件不存在视为空状态。

        Raises:
            StateError: 文件存在但内容不是合法结构时抛出。
        """

        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"读取状态文件失败: {exc}") from exc
        if not isinstance(payload, dict):
            raise StateError("状态文件顶层必须是对象")

        processed = payload.get("processed", {})
        last_trigger = payload.get("last_trigger", {})
        trigger_times = payload.get("trigger_times", [])
        if not isinstance(processed, dict) or not isinstance(last_trigger, dict) or not isinstance(trigger_times, list):
            raise StateError("状态文件字段结构不合法")

        try:
            normalized_processed = {str(key): float(value) for key, value in processed.items()}
            normalized_last_trigger = {str(key): float(value) for key, value in last_trigger.items()}
            normalized_trigger_times = [float(value) for value in trigger_times]
        except (TypeError, ValueError) as exc:
            raise StateError(f"状态文件里的时间戳不合法: {exc}") from exc

        self._processed = normalized_processed
        self._last_trigger = normalized_last_trigger
        self._trigger_times = normalized_trigger_times

    def save(self) -> None:
        """原子写回磁盘。

        Raises:
            StateError: 写入失败时抛出。
        """

        payload: Dict[str, Any] = {
            "processed": self._processed,
            "last_trigger": self._last_trigger,
            "trigger_times": self._trigger_times,
        }
        tmp_path = self._path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, self._path)
        except OSError as exc:
            raise StateError(f"写入状态文件失败: {exc}") from exc

    def prune(self, now: float) -> None:
        """丢掉过期的去重记录和过窗的额度记录。"""

        processed_deadline = now - PROCESSED_TTL_SECONDS
        self._processed = {key: value for key, value in self._processed.items() if value >= processed_deadline}
        window_deadline = now - HOURLY_WINDOW_SECONDS
        self._trigger_times = [value for value in self._trigger_times if value >= window_deadline]

    def is_processed(self, message_id: str, now: float) -> bool:
        """判断这条消息是否已经处理过。"""

        self.prune(now)
        return message_id in self._processed

    def mark_processed(self, message_id: str, now: float) -> None:
        """记下一条已处理消息。"""

        self._processed[message_id] = now

    def cooldown_remaining(self, stream_id: str, now: float, cooldown_seconds: float) -> float:
        """返回该聊天流还剩多少秒冷却。"""

        last = self._last_trigger.get(stream_id)
        if last is None:
            return 0.0
        return max(0.0, last + cooldown_seconds - now)

    def hourly_count(self, now: float) -> int:
        """返回最近一小时内的触发次数。"""

        self.prune(now)
        return len(self._trigger_times)

    def record_trigger(self, stream_id: str, now: float) -> None:
        """记录一次成功投递。"""

        self._last_trigger[stream_id] = now
        self._trigger_times.append(now)

    @staticmethod
    def now() -> float:
        """当前时间戳，单独抽出来方便测试替换。"""

        return time.time()

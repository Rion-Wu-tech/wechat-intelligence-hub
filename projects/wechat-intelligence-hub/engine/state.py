#!/usr/bin/env python3
"""WeChat Slim State & Metrics Manager.

负责记录微信瘦身工具的运行时状态、历史累计释放量、审计日志配置与 NPS 满意度反馈。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


@dataclass
class SlimHistoryRecord:
    """历史操作记录."""
    timestamp: str
    action: str            # "clean", "dedup", "archive", "scan"
    count: int
    freed_bytes: int
    protected_bytes: int = 0
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class StateManager:
    """状态与使用指标管理器."""

    def __init__(self, state_path: Optional[Union[str, Path]] = None):
        if state_path:
            self.state_path = Path(state_path).expanduser().resolve()
        else:
            self.state_path = Path.home() / ".wechat_slim_state.json"

        self.total_runs: int = 0
        self.total_scans: int = 0
        self.total_cleans: int = 0
        self.total_dedups: int = 0
        self.total_freed_bytes: int = 0
        self.total_protected_bytes: int = 0
        self.nps_score: Optional[int] = None
        self.nps_last_prompt_run: int = 0
        self.history: List[SlimHistoryRecord] = []

        self.load()

    def load(self) -> None:
        """从 JSON 加载状态记录."""
        if not self.state_path.exists():
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.total_runs = data.get("total_runs", 0)
                self.total_scans = data.get("total_scans", 0)
                self.total_cleans = data.get("total_cleans", 0)
                self.total_dedups = data.get("total_dedups", 0)
                self.total_freed_bytes = data.get("total_freed_bytes", 0)
                self.total_protected_bytes = data.get("total_protected_bytes", 0)
                self.nps_score = data.get("nps_score")
                self.nps_last_prompt_run = data.get("nps_last_prompt_run", 0)
                valid_fields = {'timestamp', 'action', 'count', 'freed_bytes', 'protected_bytes', 'note'}
                self.history = [
                    SlimHistoryRecord(**{k: v for k, v in h.items() if k in valid_fields})
                    for h in data.get("history", [])
                    if isinstance(h, dict)
                ]
        except Exception:
            # 损坏容错
            pass

    def save(self) -> None:
        """持久化保存状态到文件."""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": "1.0",
                "updated_at": datetime.now().isoformat(),
                "total_runs": self.total_runs,
                "total_scans": self.total_scans,
                "total_cleans": self.total_cleans,
                "total_dedups": self.total_dedups,
                "total_freed_bytes": self.total_freed_bytes,
                "total_protected_bytes": self.total_protected_bytes,
                "nps_score": self.nps_score,
                "nps_last_prompt_run": self.nps_last_prompt_run,
                "history": [h.to_dict() for h in self.history[-50:]],  # 保留最近 50 条
            }
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def record_scan(self) -> None:
        """记录一次扫描."""
        self.total_runs += 1
        self.total_scans += 1
        self.save()

    def record_clean(
        self,
        freed_count: int,
        freed_bytes: int,
        protected_count: int = 0,
        protected_bytes: int = 0,
        is_archive: bool = False,
    ) -> None:
        """记录一次瘦身或归档."""
        self.total_runs += 1
        self.total_cleans += 1
        self.total_freed_bytes += freed_bytes
        self.total_protected_bytes += protected_bytes
        action = "archive" if is_archive else "clean"
        rec = SlimHistoryRecord(
            timestamp=datetime.now().isoformat(),
            action=action,
            count=freed_count,
            freed_bytes=freed_bytes,
            protected_bytes=protected_bytes,
            note=f"处理 {freed_count} 个文件，跳过保护 {protected_count} 个文件",
        )
        self.history.append(rec)
        self.save()

    def record_dedup(self, processed_count: int, freed_bytes: int, action: str = "hardlink") -> None:
        """记录一次查重去重."""
        self.total_runs += 1
        self.total_dedups += 1
        self.total_freed_bytes += freed_bytes
        rec = SlimHistoryRecord(
            timestamp=datetime.now().isoformat(),
            action=f"dedup_{action}",
            count=processed_count,
            freed_bytes=freed_bytes,
            note=f"查重去重处理 {processed_count} 个副本",
        )
        self.history.append(rec)
        self.save()

    def should_trigger_nps(self) -> bool:
        """判断是否应触发 NPS 满意度反馈 (每 10 次运行或当总释放超过 5GB 且尚未反馈)."""
        if self.nps_score is not None:
            return False  # 已打分，不再打扰
        if self.total_runs >= 10 and (self.total_runs - self.nps_last_prompt_run >= 10):
            return True
        if self.total_freed_bytes >= 5 * 1024 * 1024 * 1024 and self.nps_last_prompt_run == 0:
            return True
        return False

    def mark_nps_prompted(self) -> None:
        """记录已触发过 NPS 提示."""
        self.nps_last_prompt_run = self.total_runs
        self.save()

    def record_nps(self, score: int) -> None:
        """记录用户打分 (0-10 分)."""
        self.nps_score = max(0, min(10, score))
        self.nps_last_prompt_run = self.total_runs
        self.save()

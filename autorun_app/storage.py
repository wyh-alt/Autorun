from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from autorun_app.models import ProgramEntry


class ConfigStorage:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    def load(self) -> tuple[list[ProgramEntry], str]:
        if not self.config_path.exists():
            return [], ""
        try:
            with self.config_path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            items = raw.get("programs", []) if isinstance(raw, dict) else []
            programs = [ProgramEntry.from_dict(item) for item in items if isinstance(item, dict)]
            return programs, ""
        except Exception as exc:
            return [], f"读取配置失败: {exc}"

    def save(self, programs: list[ProgramEntry]) -> str:
        data = {"programs": [item.to_dict() for item in programs]}
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=str(self.config_path.parent),
                suffix=".tmp",
            ) as tf:
                temp_file = Path(tf.name)
                json.dump(data, tf, ensure_ascii=False, indent=2)
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(str(temp_file), str(self.config_path))
            return ""
        except Exception as exc:
            if temp_file and temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass
            return f"保存配置失败: {exc}"

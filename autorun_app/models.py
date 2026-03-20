from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4


class ProgramStatus(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    ERROR = "error"


@dataclass
class ProgramEntry:
    program_id: str = field(default_factory=lambda: str(uuid4()))
    path: str = ""
    name: str = ""
    args: str = ""
    workdir: str = ""
    interpreter: str = ""
    status: ProgramStatus = ProgramStatus.STOPPED
    last_error: str = ""
    exit_code: int | None = None

    @property
    def display_name(self) -> str:
        if self.name.strip():
            return self.name.strip()
        return self.path

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "path": self.path,
            "name": self.name,
            "args": self.args,
            "workdir": self.workdir,
            "interpreter": self.interpreter,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProgramEntry":
        return cls(
            program_id=str(data.get("program_id") or str(uuid4())),
            path=str(data.get("path") or ""),
            name=str(data.get("name") or ""),
            args=str(data.get("args") or ""),
            workdir=str(data.get("workdir") or ""),
            interpreter=str(data.get("interpreter") or ""),
        )

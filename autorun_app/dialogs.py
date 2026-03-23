from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from autorun_app.models import ProgramEntry
from autorun_app.process_manager import ProcessManager


class ProgramDialog(QDialog):
    def __init__(self, parent=None, program: ProgramEntry | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("程序配置")
        self.resize(640, 280)

        self.path_input = QLineEdit()
        self.name_input = QLineEdit()
        self.args_input = QLineEdit()
        self.workdir_input = QLineEdit()
        self.interpreter_input = QLineEdit()

        path_layout = QHBoxLayout()
        path_layout.addWidget(self.path_input)
        browse_path_btn = QPushButton("浏览")
        browse_path_btn.clicked.connect(self._browse_program)
        path_layout.addWidget(browse_path_btn)

        workdir_layout = QHBoxLayout()
        workdir_layout.addWidget(self.workdir_input)
        browse_workdir_btn = QPushButton("浏览")
        browse_workdir_btn.clicked.connect(self._browse_workdir)
        workdir_layout.addWidget(browse_workdir_btn)

        interpreter_layout = QHBoxLayout()
        interpreter_layout.addWidget(self.interpreter_input)
        browse_interpreter_btn = QPushButton("浏览")
        browse_interpreter_btn.clicked.connect(self._browse_interpreter)
        interpreter_layout.addWidget(browse_interpreter_btn)

        form = QFormLayout()
        form.addRow("程序路径*", path_layout)
        form.addRow("备注名称", self.name_input)
        form.addRow("启动参数", self.args_input)
        form.addRow("工作目录", workdir_layout)
        form.addRow("解释器/运行时(可选)", interpreter_layout)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout()
        root.addLayout(form)
        root.addWidget(buttons)
        self.setLayout(root)

        self._editing_program = program
        if program:
            self.path_input.setText(program.path)
            self.name_input.setText(program.name)
            self.args_input.setText(program.args)
            self.workdir_input.setText(program.workdir)
            self.interpreter_input.setText(program.interpreter)

    def build_program(self) -> ProgramEntry:
        path = self.path_input.text().strip()
        name = self.name_input.text().strip()
        args = self.args_input.text().strip()
        workdir = self.workdir_input.text().strip()
        interpreter = self.interpreter_input.text().strip()
        if self._editing_program:
            program_id = self._editing_program.program_id
        else:
            program_id = ""
        entry = ProgramEntry(
            program_id=program_id or ProgramEntry().program_id,
            path=path,
            name=name,
            args=args,
            workdir=workdir,
            interpreter=interpreter,
        )
        return entry

    def _browse_program(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择程序")
        if path:
            self.path_input.setText(path)
            if not self.name_input.text().strip():
                self.name_input.setText(Path(path).stem)
            if not self.workdir_input.text().strip():
                self.workdir_input.setText(str(Path(path).parent))

    def _browse_workdir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择工作目录")
        if path:
            self.workdir_input.setText(path)

    def _browse_interpreter(self) -> None:
        dialog = QFileDialog(self, "选择解释器/运行时")
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setNameFilter("可执行文件 (*.exe *.bat *.cmd);;所有文件 (*)")
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        current = self.interpreter_input.text().strip()
        if current and Path(current).parent.exists():
            dialog.setDirectory(str(Path(current).parent))
        elif os.name == "nt":
            dialog.setDirectory("C:\\")
        if not dialog.exec():
            return
        selected = dialog.selectedFiles()
        if not selected:
            return
        raw_path = selected[0]
        normalized = self._normalize_interpreter_path(raw_path)
        if normalized and Path(normalized).exists():
            self.interpreter_input.setText(normalized)
            return
        QMessageBox.warning(
            self,
            "解释器路径不可用",
            f"系统无法访问该文件：\n{raw_path}\n\n请尝试选择真实 python.exe（非 WindowsApps 别名）。",
        )

    @staticmethod
    def _normalize_interpreter_path(path: str) -> str:
        candidate = Path(path)
        if candidate.exists():
            return str(candidate)
        if os.name != "nt":
            return path
        lower = str(candidate).lower()
        if candidate.name.lower() != "python.exe":
            return path
        if "windowsapps" not in lower and candidate.is_absolute():
            return path
        try:
            completed = subprocess.run(
                ["where", "python"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1.0,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            return path
        if completed.returncode != 0:
            return path
        lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        preferred = []
        fallback = []
        for line in lines:
            p = Path(line)
            if not p.exists():
                continue
            if "windowsapps" in str(p).lower():
                fallback.append(str(p))
            else:
                preferred.append(str(p))
        if preferred:
            return preferred[0]
        if fallback:
            return fallback[0]
        return path


class OutputDialog(QDialog):
    def __init__(self, parent, process_manager: ProcessManager, program: ProgramEntry) -> None:
        super().__init__(parent)
        self.process_manager = process_manager
        self.program = program
        self.last_seq = 0

        self.setWindowTitle(f"终端输出 - {program.display_name}")
        self.resize(860, 520)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.document().setMaximumBlockCount(5000)

        self.clear_btn = QPushButton("清空")
        self.close_btn = QPushButton("关闭")

        self.clear_btn.clicked.connect(self._on_clear)
        self.close_btn.clicked.connect(self.close)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.clear_btn)
        buttons.addWidget(self.close_btn)

        root = QVBoxLayout()
        root.addWidget(self.output)
        root.addLayout(buttons)
        self.setLayout(root)

        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._pull_output)
        self.timer.start()
        self._pull_output()

    def closeEvent(self, event) -> None:
        try:
            self.timer.stop()
        except Exception:
            pass
        super().closeEvent(event)

    def _pull_output(self) -> None:
        last_seq, lines = self.process_manager.get_output_since(self.program.program_id, self.last_seq)
        if lines:
            self.output.appendPlainText("\n".join(lines))
            cursor = self.output.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.output.setTextCursor(cursor)
        self.last_seq = last_seq

    def _on_clear(self) -> None:
        self.process_manager.clear_output(self.program.program_id)
        self.output.clear()
        self.last_seq = 0


class LogDialog(QDialog):
    def __init__(self, parent, process_manager: ProcessManager, program: ProgramEntry | None = None) -> None:
        super().__init__(parent)
        self.process_manager = process_manager
        self.last_seq = 0
        self._program_id = program.program_id if program else None
        self._program_name = program.display_name if program else ""

        if program:
            self.setWindowTitle(f"日志 - {program.display_name}")
        else:
            self.setWindowTitle("日志记录")
        self.resize(920, 560)

        self.hint = QLabel("格式：时间 | 程序 | 事件 | 详情")

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.document().setMaximumBlockCount(8000)
        font = QFont("Consolas")
        if font.exactMatch():
            self.output.setFont(font)

        self.export_btn = QPushButton("导出")
        self.clear_btn = QPushButton("清空")
        self.close_btn = QPushButton("关闭")

        self.export_btn.clicked.connect(self._on_export)
        self.clear_btn.clicked.connect(self._on_clear)
        self.close_btn.clicked.connect(self.close)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.export_btn)
        buttons.addWidget(self.clear_btn)
        buttons.addWidget(self.close_btn)

        root = QVBoxLayout()
        root.addWidget(self.hint)
        root.addWidget(self.output)
        root.addLayout(buttons)
        self.setLayout(root)

        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._pull_logs)
        self.timer.start()
        self._pull_logs()

    def closeEvent(self, event) -> None:
        try:
            self.timer.stop()
        except Exception:
            pass
        super().closeEvent(event)

    def _pull_logs(self) -> None:
        if self._program_id:
            last_seq, lines = self.process_manager.get_logs_since_for_program(self._program_id, self.last_seq)
        else:
            last_seq, lines = self.process_manager.get_logs_since(self.last_seq)
        if lines:
            self.output.appendPlainText("\n".join(lines))
            cursor = self.output.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.output.setTextCursor(cursor)
        self.last_seq = last_seq

    def _on_export(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        prefix = "logs"
        if self._program_name.strip():
            prefix = self._program_name.strip().replace("\\", "_").replace("/", "_").replace(":", "_")
        default_name = f"{prefix}-{stamp}.txt"
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", default_name, "Text Files (*.txt);;All Files (*)")
        if not path:
            return
        if self._program_id:
            _, lines = self.process_manager.get_logs_since_for_program(self._program_id, 0)
        else:
            _, lines = self.process_manager.get_logs_since(0)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
                if lines:
                    f.write("\n")
        except Exception as exc:
            self.output.appendPlainText(f"[导出失败] {exc}")

    def _on_clear(self) -> None:
        if self._program_id:
            self.process_manager.clear_logs_for_program(self._program_id)
        else:
            self.process_manager.clear_logs()
        self.output.clear()
        self.last_seq = 0

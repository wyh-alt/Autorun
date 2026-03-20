from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QItemSelectionModel, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from autorun_app.dialogs import LogDialog, OutputDialog, ProgramDialog
from autorun_app.models import ProgramEntry, ProgramStatus
from autorun_app.process_manager import ProcessManager
from autorun_app.storage import ConfigStorage


class StartAllWorker(QThread):
    launch_next = pyqtSignal(str)

    def __init__(self, program_ids: list[str], interval_seconds: int) -> None:
        super().__init__()
        self.program_ids = program_ids
        self.interval_seconds = max(0, interval_seconds)

    def run(self) -> None:
        for idx, pid in enumerate(self.program_ids):
            self.launch_next.emit(pid)
            if idx < len(self.program_ids) - 1 and self.interval_seconds > 0:
                self.msleep(self.interval_seconds * 1000)


class OrderTableWidget(QTableWidget):
    def __init__(self, *args, on_reorder=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._on_reorder = on_reorder

    def dropEvent(self, event) -> None:
        if event.source() is not self or not self._on_reorder:
            return super().dropEvent(event)

        selected_rows = sorted({idx.row() for idx in self.selectionModel().selectedRows()})
        if not selected_rows:
            return super().dropEvent(event)

        pos = event.position().toPoint()
        drop_row = self.indexAt(pos).row()
        if drop_row < 0:
            drop_row = self.rowCount()

        current_ids: list[str] = []
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if not item:
                continue
            pid = item.data(0x0100)
            if isinstance(pid, str):
                current_ids.append(pid)

        selected_ids: list[str] = []
        selected_row_set = set(selected_rows)
        for row in selected_rows:
            if row < 0 or row >= len(current_ids):
                continue
            selected_ids.append(current_ids[row])

        if not selected_ids:
            return super().dropEvent(event)

        removed_before = sum(1 for r in selected_row_set if r < drop_row)
        insert_at = drop_row - removed_before
        if insert_at < 0:
            insert_at = 0

        selected_id_set = set(selected_ids)
        remaining = [pid for pid in current_ids if pid not in selected_id_set]
        if insert_at > len(remaining):
            insert_at = len(remaining)

        new_order = [*remaining[:insert_at], *selected_ids, *remaining[insert_at:]]
        self._on_reorder(new_order)
        event.acceptProposedAction()


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.setWindowTitle("多程序启动管理器")
        self.resize(860, 620)
        self._app_icon_path = Path(__file__).resolve().parent / "icon.png"
        self._base_icon = QIcon(str(self._app_icon_path))
        self.setWindowIcon(self._base_icon)

        self.storage = ConfigStorage(config_path)
        self.process_manager = ProcessManager()
        self.programs: list[ProgramEntry] = []
        self.start_all_worker: StartAllWorker | None = None
        self._output_dialogs: dict[str, OutputDialog] = {}
        self._log_dialog: LogDialog | None = None
        self._program_log_dialogs: dict[str, LogDialog] = {}
        self._tray: QSystemTrayIcon | None = None
        self._allow_close: bool = False
        self._close_action_preference: str | None = None

        self._build_ui()
        min_width = self.minimumSizeHint().width()
        self.setMinimumWidth(min_width)
        self.resize(min_width, self.height())
        self._load_config()
        self._start_polling()
        self._update_tray()

    def _build_ui(self) -> None:
        self.table = OrderTableWidget(0, 6, on_reorder=self._on_table_reorder)
        self.table.setHorizontalHeaderLabels(["顺序", "备注名称", "状态", "CPU利用率", "显存占用", "错误信息"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setSortingEnabled(False)
        self.table.itemDoubleClicked.connect(self._on_open_output_from_item)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.viewport().setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.table.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.table.setDragDropOverwriteMode(False)

        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(40)
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 95)
        self.table.setColumnWidth(4, 95)
        self.table.setStyleSheet(
            "QTableWidget::item:selected { background-color: palette(highlight); color: palette(highlighted-text); }"
            "QHeaderView::section { padding: 6px; }"
        )

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(0, 3600)
        self.interval_spin.setValue(1)
        self.interval_spin.setSuffix(" 秒")
        self.interval_spin.setMinimumWidth(90)

        self.add_btn = QPushButton("添加")
        self.edit_btn = QPushButton("编辑")
        self.delete_btn = QPushButton("删除")
        self.move_up_btn = QPushButton("上移")
        self.move_down_btn = QPushButton("下移")
        self.show_terminal_btn = QPushButton("显示终端")
        self.log_btn = QPushButton("日志")
        self.run_selected_btn = QPushButton("运行选中")
        self.stop_selected_btn = QPushButton("停止选中")
        self.kill_selected_btn = QPushButton("停止所有")
        self.run_all_btn = QPushButton("运行所有")

        for btn in (
            self.add_btn,
            self.edit_btn,
            self.delete_btn,
            self.move_up_btn,
            self.move_down_btn,
            self.show_terminal_btn,
            self.log_btn,
        ):
            btn.setMinimumWidth(70)
        for btn in (self.run_selected_btn, self.stop_selected_btn, self.kill_selected_btn, self.run_all_btn):
            btn.setMinimumWidth(100)

        self.add_btn.clicked.connect(self._on_add_program)
        self.edit_btn.clicked.connect(self._on_edit_program)
        self.delete_btn.clicked.connect(self._on_delete_program)
        self.move_up_btn.clicked.connect(self._on_move_up)
        self.move_down_btn.clicked.connect(self._on_move_down)
        self.show_terminal_btn.clicked.connect(self._on_show_terminal)
        self.log_btn.clicked.connect(self._on_show_logs)
        self.run_selected_btn.clicked.connect(self._on_run_selected)
        self.stop_selected_btn.clicked.connect(self._on_stop_selected)
        self.kill_selected_btn.clicked.connect(self._on_stop_all)
        self.run_all_btn.clicked.connect(self._on_run_all)

        controls_grid = QGridLayout()
        controls_grid.setHorizontalSpacing(8)
        controls_grid.setVerticalSpacing(6)
        controls_grid.addWidget(self.add_btn, 0, 0)
        controls_grid.addWidget(self.edit_btn, 0, 1)
        controls_grid.addWidget(self.delete_btn, 0, 2)
        controls_grid.addWidget(self.move_up_btn, 0, 3)
        controls_grid.addWidget(self.move_down_btn, 0, 4)
        controls_grid.addWidget(self.show_terminal_btn, 0, 5)
        controls_grid.addWidget(self.log_btn, 0, 6)
        controls_grid.addWidget(QLabel("顺序启动间隔"), 1, 0)
        controls_grid.addWidget(self.interval_spin, 1, 1)
        controls_grid.addWidget(self.run_selected_btn, 1, 2)
        controls_grid.addWidget(self.stop_selected_btn, 1, 3)
        controls_grid.addWidget(self.kill_selected_btn, 1, 4)
        controls_grid.addWidget(self.run_all_btn, 1, 5, 1, 2)
        controls_grid.setColumnStretch(7, 1)

        root = QVBoxLayout()
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        root.addWidget(self.table)
        root.addLayout(controls_grid)

        container = QWidget()
        container.setLayout(root)
        self.setCentralWidget(container)

        self._setup_tray()

    def _load_config(self) -> None:
        programs, error = self.storage.load()
        self.programs = programs
        self.process_manager.set_programs(self.programs)
        self._refresh_table()
        if error:
            self._notify(error, error=True)
        self._update_tray()

    def _save_config(self) -> None:
        message = self.storage.save(self.programs)
        if message:
            self._notify(message, error=True)

    def _start_polling(self) -> None:
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(1000)
        self.poll_timer.timeout.connect(self._on_poll_tick)
        self.poll_timer.start()

    def _on_poll_tick(self) -> None:
        self.process_manager.poll_programs(self.programs)
        self.process_manager.refresh_metrics(self.programs)
        if self.table.state() != QAbstractItemView.State.DraggingState:
            self._refresh_table(keep_selection=True)
        self._update_tray()

    def _on_add_program(self) -> None:
        dialog = ProgramDialog(self)
        if dialog.exec():
            new_program = dialog.build_program()
            if not new_program.path.strip():
                self._notify("程序路径不能为空", error=True)
                return
            self._auto_fill_interpreter(new_program)
            self.programs.append(new_program)
            self.process_manager.set_programs(self.programs)
            self._save_config()
            self._refresh_table()

    def _on_edit_program(self) -> None:
        selected = self._get_selected_programs()
        if len(selected) != 1:
            self._notify("请仅选择一条程序进行编辑", error=True)
            return
        current = selected[0]
        dialog = ProgramDialog(self, current)
        if dialog.exec():
            updated = dialog.build_program()
            if not updated.path.strip():
                self._notify("程序路径不能为空", error=True)
                return
            self._auto_fill_interpreter(updated)
            current.path = updated.path
            current.name = updated.name
            current.args = updated.args
            current.workdir = updated.workdir
            current.interpreter = updated.interpreter
            self._save_config()
            self._refresh_table(keep_selection=True)

    def _is_python_program(self, program: ProgramEntry) -> bool:
        return Path(program.path).suffix.lower() in {".py", ".pyw"}

    def _find_interpreter_candidate(self, program: ProgramEntry) -> Path | None:
        if not self._is_python_program(program):
            return None
        script_dir = Path(program.path).parent
        bases: list[Path] = []
        if program.workdir.strip():
            bases.append(Path(program.workdir.strip()))
        bases.append(script_dir)
        for base in list(bases):
            cursor = base
            for _ in range(3):
                parent = cursor.parent
                if parent == cursor:
                    break
                bases.append(parent)
                cursor = parent
        seen: set[str] = set()
        for base in bases:
            key = str(base).lower()
            if key in seen:
                continue
            seen.add(key)
            for env_name in (".venv", "venv", "env", "workenv"):
                candidate = base / env_name / "Scripts" / "python.exe"
                if candidate.exists():
                    return candidate
        return None

    def _auto_fill_interpreter(self, program: ProgramEntry) -> None:
        if program.interpreter.strip() or not self._is_python_program(program):
            return
        found = self._find_interpreter_candidate(program)
        if found:
            program.interpreter = str(found)

    def _ensure_interpreter_before_run(self, program: ProgramEntry) -> bool:
        if not self._is_python_program(program):
            return True
        if program.interpreter.strip():
            return True
        self._auto_fill_interpreter(program)
        if program.interpreter.strip():
            self._save_config()
            return True
        choice = QMessageBox.question(
            self,
            "解释器提醒",
            "当前 .py 程序未设置解释器，可能会使用默认 Python 运行。\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return choice == QMessageBox.StandardButton.Yes

    def _ensure_interpreters_before_run_all(self, programs: list[ProgramEntry]) -> bool:
        missing: list[ProgramEntry] = []
        changed = False
        for program in programs:
            if not self._is_python_program(program):
                continue
            before = program.interpreter.strip()
            if not before:
                self._auto_fill_interpreter(program)
            after = program.interpreter.strip()
            if not before and after:
                changed = True
            if not after:
                missing.append(program)
        if changed:
            self._save_config()
        if not missing:
            return True
        lines = []
        for idx, program in enumerate(missing, start=1):
            title = program.display_name.strip() or f"程序{idx}"
            lines.append(f"{idx}. {title} | {program.path}")
        message = "以下 .py 程序未设置解释器，可能会使用默认 Python 运行：\n\n" + "\n".join(lines) + "\n\n是否继续运行所有？"
        choice = QMessageBox.question(
            self,
            "解释器提醒",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return choice == QMessageBox.StandardButton.Yes

    def _on_delete_program(self) -> None:
        selected = self._get_selected_programs()
        if not selected:
            self._notify("请先选择要删除的程序", error=True)
            return
        for program in selected:
            self.process_manager.stop_program(program, force=True)
            self.programs = [p for p in self.programs if p.program_id != program.program_id]
        self.process_manager.set_programs(self.programs)
        self._save_config()
        self._refresh_table()

    def _on_move_up(self) -> None:
        selected = self._get_selected_programs()
        if not selected:
            self._notify("请先选择要移动的程序", error=True)
            return
        selected_ids = [p.program_id for p in selected]
        id_to_index = {p.program_id: i for i, p in enumerate(self.programs)}
        indices = sorted([id_to_index[pid] for pid in selected_ids if pid in id_to_index])
        if not indices:
            return
        selected_index_set = set(indices)
        changed = False
        for idx in indices:
            if idx <= 0:
                continue
            if (idx - 1) in selected_index_set:
                continue
            self.programs[idx - 1], self.programs[idx] = self.programs[idx], self.programs[idx - 1]
            moved_id = self.programs[idx - 1].program_id
            selected_index_set.remove(idx)
            selected_index_set.add(idx - 1)
            id_to_index[moved_id] = idx - 1
            changed = True
        if changed:
            self._save_config()
            self._refresh_table()
            self._select_program_ids(set(selected_ids))

    def _on_move_down(self) -> None:
        selected = self._get_selected_programs()
        if not selected:
            self._notify("请先选择要移动的程序", error=True)
            return
        selected_ids = [p.program_id for p in selected]
        id_to_index = {p.program_id: i for i, p in enumerate(self.programs)}
        indices = sorted([id_to_index[pid] for pid in selected_ids if pid in id_to_index], reverse=True)
        if not indices:
            return
        selected_index_set = set(indices)
        changed = False
        for idx in indices:
            if idx >= len(self.programs) - 1:
                continue
            if (idx + 1) in selected_index_set:
                continue
            self.programs[idx + 1], self.programs[idx] = self.programs[idx], self.programs[idx + 1]
            moved_id = self.programs[idx + 1].program_id
            selected_index_set.remove(idx)
            selected_index_set.add(idx + 1)
            id_to_index[moved_id] = idx + 1
            changed = True
        if changed:
            self._save_config()
            self._refresh_table()
            self._select_program_ids(set(selected_ids))

    def _on_run_selected(self) -> None:
        selected = self._get_selected_programs()
        if not selected:
            self._notify("请先选择要运行的程序", error=True)
            return
        for program in selected:
            if not self._ensure_interpreter_before_run(program):
                return
        fail_count = 0
        for program in selected:
            ok, _ = self.process_manager.start_program(program)
            if not ok:
                fail_count += 1
        self._refresh_table(keep_selection=True)
        if fail_count:
            self._notify(f"已尝试运行，失败 {fail_count} 项", error=True)
        else:
            self._notify(f"已运行 {len(selected)} 项")

    def _on_stop_selected(self) -> None:
        self._stop_selected(force=False)

    def _on_kill_selected(self) -> None:
        self._stop_selected(force=True)

    def _on_stop_all(self) -> None:
        if not self.programs:
            self._notify("当前没有可停止的程序", error=True)
            return
        choice = QMessageBox.question(
            self,
            "确认操作",
            "是否需终止所有程序？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        fail_count = 0
        for program in self.programs:
            ok, _ = self.process_manager.stop_program(program, force=True)
            if not ok:
                fail_count += 1
        self.process_manager.poll_programs(self.programs)
        self._refresh_table(keep_selection=True)
        if fail_count:
            self._notify(f"停止所有完成，失败 {fail_count} 项", error=True)

    def _stop_selected(self, force: bool) -> None:
        selected = self._get_selected_programs()
        if not selected:
            self._notify("请先选择要停止的程序", error=True)
            return
        fail_count = 0
        for program in selected:
            ok, _ = self.process_manager.stop_program(program, force=force)
            if not ok:
                fail_count += 1
        self.process_manager.poll_programs(self.programs)
        self._refresh_table(keep_selection=True)
        if fail_count:
            self._notify(f"停止操作完成，失败 {fail_count} 项", error=True)
        else:
            self._notify(f"已停止 {len(selected)} 项")

    def _on_run_all(self) -> None:
        if not self.programs:
            self._notify("当前没有可运行的程序", error=True)
            return
        if not self._ensure_interpreters_before_run_all(self.programs):
            return
        if self.start_all_worker and self.start_all_worker.isRunning():
            self._notify("运行所有任务正在执行中", error=True)
            return
        ids = [program.program_id for program in self.programs]
        interval = int(self.interval_spin.value())
        self.start_all_worker = StartAllWorker(ids, interval)
        self.start_all_worker.launch_next.connect(self._run_by_id)
        self.start_all_worker.finished.connect(lambda: self._notify("运行所有执行完成"))
        self.start_all_worker.start()
        self._notify("开始按顺序启动所有程序")

    def _on_show_terminal(self) -> None:
        selected = self._get_selected_programs()
        if len(selected) != 1:
            self._notify("请仅选择一条程序查看终端输出", error=True)
            return
        self._open_output_dialog(selected[0])

    def _on_show_logs(self) -> None:
        if self._log_dialog and self._log_dialog.isVisible():
            self._log_dialog.raise_()
            self._log_dialog.activateWindow()
            return
        dialog = LogDialog(self, self.process_manager)
        self._log_dialog = dialog
        dialog.finished.connect(lambda _: setattr(self, "_log_dialog", None))
        dialog.show()

    def _on_show_program_logs(self, program: ProgramEntry) -> None:
        existing = self._program_log_dialogs.get(program.program_id)
        if existing and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        dialog = LogDialog(self, self.process_manager, program=program)
        self._program_log_dialogs[program.program_id] = dialog
        dialog.finished.connect(lambda _: self._program_log_dialogs.pop(program.program_id, None))
        dialog.show()

    def _on_table_reorder(self, program_ids: list[str]) -> None:
        by_id = {program.program_id: program for program in self.programs}
        new_programs: list[ProgramEntry] = []
        for pid in program_ids:
            program = by_id.get(pid)
            if program:
                new_programs.append(program)
        remaining = [p for p in self.programs if p.program_id not in {p.program_id for p in new_programs}]
        self.programs = [*new_programs, *remaining]
        self._save_config()
        self._refresh_table(keep_selection=True)

    def _on_open_output_from_item(self, item: QTableWidgetItem) -> None:
        row = item.row()
        id_item = self.table.item(row, 0)
        if not id_item:
            return
        program_id = id_item.data(0x0100)
        if not isinstance(program_id, str):
            return
        program = next((p for p in self.programs if p.program_id == program_id), None)
        if not program:
            return
        self._open_output_dialog(program)

    def _on_table_context_menu(self, pos) -> None:
        row = self.table.indexAt(pos).row()
        if row >= 0:
            selection = self.table.selectionModel()
            if selection and not selection.isRowSelected(row, self.table.rootIndex()):
                selection.clearSelection()
                selection.select(
                    self.table.model().index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                )

        selected = self._get_selected_programs()
        has_selection = bool(selected)
        single = len(selected) == 1

        menu = QMenu(self)
        run_action = menu.addAction("启动/运行")
        stop_action = menu.addAction("终止(正常)")
        kill_action = menu.addAction("强制终止")
        menu.addSeparator()
        terminal_action = menu.addAction("显示终端")
        program_logs_action = menu.addAction("日志(此程序)")
        edit_action = menu.addAction("编辑")
        delete_action = menu.addAction("删除")
        menu.addSeparator()
        up_action = menu.addAction("上移")
        down_action = menu.addAction("下移")

        run_action.setEnabled(has_selection)
        stop_action.setEnabled(has_selection)
        kill_action.setEnabled(has_selection)
        terminal_action.setEnabled(single)
        program_logs_action.setEnabled(single)
        edit_action.setEnabled(single)
        delete_action.setEnabled(has_selection)
        up_action.setEnabled(has_selection)
        down_action.setEnabled(has_selection)

        run_action.triggered.connect(self._on_run_selected)
        stop_action.triggered.connect(self._on_stop_selected)
        kill_action.triggered.connect(self._on_kill_selected)
        terminal_action.triggered.connect(self._on_show_terminal)
        if single:
            program_logs_action.triggered.connect(lambda: self._on_show_program_logs(self._get_selected_programs()[0]))
        edit_action.triggered.connect(self._on_edit_program)
        delete_action.triggered.connect(self._on_delete_program)
        up_action.triggered.connect(self._on_move_up)
        down_action.triggered.connect(self._on_move_down)

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _open_output_dialog(self, program: ProgramEntry) -> None:
        existing = self._output_dialogs.get(program.program_id)
        if existing and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        dialog = OutputDialog(self, self.process_manager, program)
        self._output_dialogs[program.program_id] = dialog
        dialog.finished.connect(lambda _: self._output_dialogs.pop(program.program_id, None))
        dialog.show()

    def _run_by_id(self, program_id: str) -> None:
        program = next((p for p in self.programs if p.program_id == program_id), None)
        if not program:
            return
        self.process_manager.start_program(program)
        self._refresh_table(keep_selection=True)

    def _get_selected_programs(self) -> list[ProgramEntry]:
        selected_rows = sorted({idx.row() for idx in self.table.selectionModel().selectedRows()})
        selected_ids: list[str] = []
        for row in selected_rows:
            item = self.table.item(row, 0)
            if not item:
                continue
            program_id = item.data(0x0100)
            if isinstance(program_id, str):
                selected_ids.append(program_id)
        by_id = {program.program_id: program for program in self.programs}
        return [by_id[pid] for pid in selected_ids if pid in by_id]

    def _refresh_table(self, keep_selection: bool = False) -> None:
        selected_ids = {program.program_id for program in self._get_selected_programs()} if keep_selection else set()
        visible = list(self.programs)
        self.table.setRowCount(len(visible))
        for row, program in enumerate(visible):
            order_item = QTableWidgetItem(str(row + 1))
            order_item.setData(0x0100, program.program_id)
            order_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            name_text = program.name.strip()
            if not name_text and program.path.strip():
                try:
                    name_text = Path(program.path).stem
                except Exception:
                    name_text = ""
            if not name_text:
                name_text = "(未命名)"
            name_item = QTableWidgetItem(name_text)
            status_text, color = self._status_view(program)
            status_item = QTableWidgetItem(status_text)
            cpu_percent = self.process_manager.get_cpu_percent(program.program_id)
            cpu_item = QTableWidgetItem("—" if cpu_percent is None else f"{cpu_percent:.1f}%")
            gpu_mem = self.process_manager.get_gpu_mem_mib(program.program_id)
            vram_item = QTableWidgetItem("—" if gpu_mem is None else f"{gpu_mem} MiB")
            error_item = QTableWidgetItem(program.last_error)
            status_item.setForeground(QBrush(color))
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            cpu_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            vram_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            status_font = QFont()
            status_font.setBold(True)
            status_item.setFont(status_font)

            name_item.setToolTip(program.path)
            error_item.setToolTip(program.last_error)

            self.table.setItem(row, 0, order_item)
            self.table.setItem(row, 1, name_item)
            self.table.setItem(row, 2, status_item)
            self.table.setItem(row, 3, cpu_item)
            self.table.setItem(row, 4, vram_item)
            self.table.setItem(row, 5, error_item)

        if keep_selection and selected_ids:
            self._select_program_ids(selected_ids)

    def _select_program_ids(self, program_ids: set[str]) -> None:
        if not program_ids:
            return
        selection = self.table.selectionModel()
        if selection:
            selection.clearSelection()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(0x0100) in program_ids:
                if selection:
                    selection.select(
                        self.table.model().index(row, 0),
                        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                    )

    def _status_view(self, program: ProgramEntry) -> tuple[str, QColor]:
        if program.status == ProgramStatus.RUNNING:
            return "● 运行中", QColor("#19a65a")
        if program.status == ProgramStatus.ERROR:
            return "● 异常", QColor("#c08a00")
        return "● 未运行", QColor("#d93025")

    def _notify(self, message: str, error: bool = False) -> None:
        if error:
            QMessageBox.warning(self, "提示", message)
        else:
            return

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self)
        tray.setToolTip("多程序启动管理器")

        menu = QMenu(self)
        toggle_action = QAction("显示/隐藏", self)
        exit_action = QAction("退出程序", self)
        toggle_action.triggered.connect(self._toggle_window_visibility)
        exit_action.triggered.connect(self._exit_app_from_tray)
        menu.addAction(toggle_action)
        menu.addSeparator()
        menu.addAction(exit_action)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    def _toggle_window_visibility(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self._show_window()

    def _on_tray_activated(self, reason) -> None:
        if reason in {QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick}:
            self._show_window()

    def _show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _exit_app_from_tray(self) -> None:
        if not self._confirm_exit_if_running():
            return
        self._allow_close = True
        self.close()

    def _confirm_exit_if_running(self) -> bool:
        if not self.process_manager.has_running(self.programs):
            return True
        choice = QMessageBox.question(
            self,
            "退出确认",
            "检测到有程序正在运行，是否终止所有程序并退出？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return choice == QMessageBox.StandardButton.Yes

    def _tray_status(self) -> tuple[str, QColor]:
        total = len(self.programs)
        running = sum(1 for p in self.programs if p.status == ProgramStatus.RUNNING)
        error = sum(1 for p in self.programs if p.status == ProgramStatus.ERROR)
        if error:
            return f"异常 {error} | 运行 {running} | 总 {total}", QColor("#f9ab00")
        if running:
            return f"运行 {running} | 总 {total}", QColor("#34a853")
        return f"未运行 | 总 {total}", QColor("#ea4335")

    def _build_tray_icon(self, dot_color: QColor) -> QIcon:
        size = 64
        source = QPixmap(str(self._app_icon_path))
        if source.isNull():
            pix = QPixmap(size, size)
            pix.fill(Qt.GlobalColor.transparent)
        else:
            pix = source.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            if pix.width() != size or pix.height() != size:
                fitted = QPixmap(size, size)
                fitted.fill(Qt.GlobalColor.transparent)
                painter_bg = QPainter(fitted)
                x = (size - pix.width()) // 2
                y = (size - pix.height()) // 2
                painter_bg.drawPixmap(x, y, pix)
                painter_bg.end()
                pix = fitted

        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        cx = size - 18
        cy = 18
        r = 10
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.setBrush(dot_color)
        painter.drawEllipse(cx - r, cy - r, r * 2, r * 2)

        painter.end()
        return QIcon(pix)

    def _update_tray(self) -> None:
        if not self._tray:
            return
        tip, color = self._tray_status()
        self._tray.setToolTip(f"多程序启动管理器\n{tip}")
        self._tray.setIcon(self._build_tray_icon(color))

    def closeEvent(self, event) -> None:
        if not self._allow_close:
            if self._close_action_preference == "hide":
                event.ignore()
                self.hide()
                if self._tray:
                    self._tray.showMessage("多程序启动管理器", "已隐藏至托盘，双击图标可恢复窗口。")
                return
            if self._close_action_preference == "exit":
                if not self._confirm_exit_if_running():
                    event.ignore()
                    return
                self._allow_close = True
            if not self._allow_close:
                box = QMessageBox(self)
                box.setWindowTitle("关闭")
                box.setText("请选择操作：\n注意：退出程序将终止所有运行中的程序。")
                remember_check = QCheckBox("记住本次选择")
                box.setCheckBox(remember_check)
                hide_btn = box.addButton("隐藏至托盘", QMessageBox.ButtonRole.AcceptRole)
                exit_btn = box.addButton("退出程序", QMessageBox.ButtonRole.DestructiveRole)
                cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
                box.setDefaultButton(hide_btn)
                box.exec()
                clicked = box.clickedButton()
                if clicked is cancel_btn:
                    event.ignore()
                    return
                if clicked is hide_btn:
                    if remember_check.isChecked():
                        self._close_action_preference = "hide"
                    event.ignore()
                    self.hide()
                    if self._tray:
                        self._tray.showMessage("多程序启动管理器", "已隐藏至托盘，双击图标可恢复窗口。")
                    return
                if clicked is exit_btn:
                    if remember_check.isChecked():
                        self._close_action_preference = "exit"
                    if not self._confirm_exit_if_running():
                        event.ignore()
                        return
                    self._allow_close = True
        if self.start_all_worker and self.start_all_worker.isRunning():
            self.start_all_worker.terminate()
            self.start_all_worker.wait(300)
        self.process_manager.shutdown_all(self.programs)
        self._save_config()
        if self._tray:
            self._tray.hide()
        super().closeEvent(event)

    def show_error_dialog(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

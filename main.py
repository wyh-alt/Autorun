from __future__ import annotations

import atexit
import ctypes
import os
import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from autorun_app.main_window import MainWindow


_SINGLE_INSTANCE_HANDLE: int | None = None


def _ensure_single_instance() -> bool:
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    mutex_name = "Global\\Autorun.SingleInstance"
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        return True
    already_exists = kernel32.GetLastError() == 183
    if already_exists:
        user32.MessageBoxW(None, "多程序启动管理器正处于运行中。", "提示", 0x30)
        kernel32.CloseHandle(handle)
        return False

    global _SINGLE_INSTANCE_HANDLE
    _SINGLE_INSTANCE_HANDLE = int(handle)

    def _release_mutex() -> None:
        if _SINGLE_INSTANCE_HANDLE:
            kernel32.CloseHandle(_SINGLE_INSTANCE_HANDLE)

    atexit.register(_release_mutex)
    return True


def main() -> int:
    if not _ensure_single_instance():
        return 0
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("autorun.app")
        except Exception:
            pass
    app = QApplication(sys.argv)
    root_dir = Path(__file__).resolve().parent
    icon_path = root_dir / "autorun_app" / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    config_path = root_dir / "config.json"
    window = MainWindow(config_path=config_path)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

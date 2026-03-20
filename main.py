from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from autorun_app.main_window import MainWindow


def main() -> int:
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

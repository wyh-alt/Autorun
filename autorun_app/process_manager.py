from __future__ import annotations

import ctypes
import json
import locale
import os
import re
import shlex
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from pathlib import Path

from autorun_app.models import ProgramEntry, ProgramStatus


@dataclass(frozen=True)
class LogEntry:
    seq: int
    ts: float
    program_id: str
    program_name: str
    event: str
    detail: str

    def format_line(self) -> str:
        dt = datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M:%S")
        name = self.program_name.strip() or self.program_id
        detail = self.detail.strip()
        if detail:
            return f"{dt} | {name} | {self.event} | {detail}"
        return f"{dt} | {name} | {self.event}"


@dataclass
class RuntimeState:
    process: subprocess.Popen | None = None
    output: deque[tuple[int, str]] = field(default_factory=lambda: deque(maxlen=5000))
    output_seq: int = 0
    reader_thread: threading.Thread | None = None
    cpu_percent: float | None = None
    _cpu_last_check: float | None = None
    _cpu_last_time_100ns: int | None = None
    gpu_mem_mib: int | None = None
    stop_requested: bool = False
    runtime_error_detected: bool = False
    runtime_error_message: str = ""
    runtime_error_logged: bool = False
    traceback_pending: bool = False


class ProcessManager:
    def __init__(self) -> None:
        self._runtime: dict[str, RuntimeState] = {}
        self._lock = threading.RLock()
        self._gpu_last_refresh: float = 0.0
        self._gpu_pid_mem_mib: dict[int, int] = {}
        self._gpu_global_mem_mib: int | None = None
        self._logs: deque[LogEntry] = deque(maxlen=8000)
        self._log_seq: int = 0

    def set_programs(self, programs: list[ProgramEntry]) -> None:
        with self._lock:
            existing_ids = {p.program_id for p in programs}
            for program in programs:
                self._runtime.setdefault(program.program_id, RuntimeState())
            stale = [pid for pid in self._runtime if pid not in existing_ids]
            for pid in stale:
                state = self._runtime[pid]
                if state.process and state.process.poll() is None:
                    try:
                        state.process.terminate()
                    except Exception:
                        pass
                self._runtime.pop(pid, None)

    def start_program(self, program: ProgramEntry) -> tuple[bool, str]:
        with self._lock:
            state = self._runtime.setdefault(program.program_id, RuntimeState())
            if state.process and state.process.poll() is None:
                program.status = ProgramStatus.RUNNING
                program.last_error = ""
                self._append_log(program, event="启动", detail="已在运行")
                return True, ""
            if not program.path.strip():
                program.status = ProgramStatus.ERROR
                program.last_error = "程序路径为空"
                self._append_log(program, event="启动失败", detail=program.last_error)
                return False, program.last_error
            exec_path = Path(program.path)
            if not exec_path.exists():
                program.status = ProgramStatus.ERROR
                program.last_error = "程序路径不存在"
                self._append_log(program, event="启动失败", detail=program.last_error)
                return False, program.last_error
            cmd = self._build_command(program)
            if not cmd:
                program.status = ProgramStatus.ERROR
                program.last_error = "命令构建失败"
                self._append_log(program, event="启动失败", detail=program.last_error)
                return False, program.last_error
            try:
                cwd = program.workdir.strip() or str(exec_path.parent)
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                if os.name == "nt":
                    creationflags |= subprocess.CREATE_NO_WINDOW
                launch_env = os.environ.copy()
                is_python_script = exec_path.suffix.lower() in {".py", ".pyw"}
                if exec_path.suffix.lower() in {".py", ".pyw"}:
                    launch_env["PYTHONUTF8"] = "1"
                    launch_env["PYTHONIOENCODING"] = "utf-8"
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    env=launch_env,
                    creationflags=creationflags,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                state.process = proc
                state.output.clear()
                state.output_seq = 0
                state.cpu_percent = None
                state._cpu_last_check = None
                state._cpu_last_time_100ns = None
                state.gpu_mem_mib = None
                state.runtime_error_detected = False
                state.runtime_error_message = ""
                state.runtime_error_logged = False
                state.traceback_pending = False
                state.reader_thread = threading.Thread(
                    target=self._read_output,
                    args=(program.program_id, proc),
                    daemon=True,
                )
                state.reader_thread.start()
                program.status = ProgramStatus.RUNNING
                program.last_error = ""
                program.exit_code = None
                self._append_log(program, event="启动", detail=f"PID {proc.pid}")
                return True, ""
            except Exception as exc:
                state.process = None
                program.status = ProgramStatus.ERROR
                program.last_error = f"启动失败: {exc}"
                program.exit_code = None
                self._append_log(program, event="启动失败", detail=str(exc))
                return False, program.last_error

    def stop_program(self, program: ProgramEntry, force: bool = False) -> tuple[bool, str]:
        with self._lock:
            state = self._runtime.setdefault(program.program_id, RuntimeState())
            proc = state.process
            if not proc or proc.poll() is not None:
                state.process = None
                state.cpu_percent = None
                state._cpu_last_check = None
                state._cpu_last_time_100ns = None
                state.gpu_mem_mib = None
                state.stop_requested = False
                state.runtime_error_detected = False
                state.runtime_error_message = ""
                state.runtime_error_logged = False
                state.traceback_pending = False
                program.status = ProgramStatus.STOPPED
                program.last_error = ""
                self._append_log(program, event="终止", detail="未在运行")
                return True, ""
            try:
                state.stop_requested = True
                if force:
                    proc.kill()
                    self._append_log(program, event="强制终止", detail=f"PID {proc.pid}")
                else:
                    proc.terminate()
                    self._append_log(program, event="终止", detail=f"PID {proc.pid}")
                return True, ""
            except Exception as exc:
                program.status = ProgramStatus.ERROR
                program.last_error = f"停止失败: {exc}"
                self._append_log(program, event="终止失败", detail=str(exc))
                return False, program.last_error

    def poll_programs(self, programs: list[ProgramEntry]) -> None:
        with self._lock:
            for program in programs:
                state = self._runtime.setdefault(program.program_id, RuntimeState())
                proc = state.process
                if not proc:
                    if program.status == ProgramStatus.RUNNING:
                        self._append_log(program, event="退出", detail="进程不存在/已结束")
                    if program.status != ProgramStatus.ERROR:
                        program.status = ProgramStatus.STOPPED
                    state.cpu_percent = None
                    state._cpu_last_check = None
                    state._cpu_last_time_100ns = None
                    state.gpu_mem_mib = None
                    state.stop_requested = False
                    state.runtime_error_detected = False
                    state.runtime_error_message = ""
                    state.runtime_error_logged = False
                    state.traceback_pending = False
                    continue
                code = proc.poll()
                if code is None:
                    if state.runtime_error_detected:
                        program.status = ProgramStatus.ERROR
                        if state.runtime_error_message:
                            program.last_error = state.runtime_error_message
                        if not state.runtime_error_logged:
                            self._append_log(program, event="运行异常", detail=state.runtime_error_message or "检测到异常堆栈")
                            state.runtime_error_logged = True
                    else:
                        program.status = ProgramStatus.RUNNING
                    continue
                state.process = None
                state.cpu_percent = None
                state._cpu_last_check = None
                state._cpu_last_time_100ns = None
                state.gpu_mem_mib = None
                program.exit_code = code
                if state.stop_requested:
                    program.status = ProgramStatus.STOPPED
                    program.last_error = ""
                    self._append_log(program, event="退出", detail=f"停止后退出，返回码 {code}")
                elif state.runtime_error_detected:
                    program.status = ProgramStatus.ERROR
                    program.last_error = state.runtime_error_message or f"运行异常退出，返回码: {code}"
                    self._append_log(program, event="异常退出", detail=program.last_error)
                elif code == 0:
                    program.status = ProgramStatus.STOPPED
                    program.last_error = ""
                    self._append_log(program, event="退出", detail="返回码 0")
                else:
                    program.status = ProgramStatus.ERROR
                    program.last_error = f"运行异常退出，返回码: {code}"
                    self._append_log(program, event="异常退出", detail=f"返回码 {code}")
                state.stop_requested = False
                state.runtime_error_detected = False
                state.runtime_error_message = ""
                state.runtime_error_logged = False
                state.traceback_pending = False

    def shutdown_all(self, programs: list[ProgramEntry]) -> None:
        with self._lock:
            for program in programs:
                state = self._runtime.get(program.program_id)
                if not state or not state.process:
                    continue
                if state.process.poll() is None:
                    try:
                        state.stop_requested = True
                        state.process.terminate()
                        self._append_log(program, event="终止", detail="退出程序时关闭")
                    except Exception:
                        pass
                state.cpu_percent = None
                state._cpu_last_check = None
                state._cpu_last_time_100ns = None
                state.gpu_mem_mib = None
                state.stop_requested = False
                state.runtime_error_detected = False
                state.runtime_error_message = ""
                state.runtime_error_logged = False
                state.traceback_pending = False

    def get_output_since(self, program_id: str, last_seq: int) -> tuple[int, list[str]]:
        with self._lock:
            state = self._runtime.get(program_id)
            if not state:
                return last_seq, []
            newest = state.output_seq
            if not state.output:
                return newest, []
            lines = [text for seq, text in state.output if seq > last_seq]
            return newest, lines

    def clear_output(self, program_id: str) -> None:
        with self._lock:
            state = self._runtime.get(program_id)
            if not state:
                return
            state.output.clear()
            state.output_seq = 0

    def get_logs_since(self, last_seq: int) -> tuple[int, list[str]]:
        with self._lock:
            newest = self._log_seq
            if not self._logs:
                return newest, []
            lines = [entry.format_line() for entry in self._logs if entry.seq > last_seq]
            return newest, lines

    def clear_logs(self) -> None:
        with self._lock:
            self._logs.clear()
            self._log_seq = 0

    def clear_logs_for_program(self, program_id: str) -> None:
        with self._lock:
            if not self._logs:
                return
            self._logs = deque((e for e in self._logs if e.program_id != program_id), maxlen=8000)

    def get_logs_since_for_program(self, program_id: str, last_seq: int) -> tuple[int, list[str]]:
        with self._lock:
            newest = self._log_seq
            if not self._logs:
                return newest, []
            lines = [
                entry.format_line()
                for entry in self._logs
                if entry.seq > last_seq and entry.program_id == program_id
            ]
            return newest, lines

    def has_running(self, programs: list[ProgramEntry]) -> bool:
        with self._lock:
            for program in programs:
                state = self._runtime.get(program.program_id)
                if state and state.process and state.process.poll() is None:
                    return True
            return False

    def refresh_metrics(self, programs: list[ProgramEntry]) -> None:
        now = time.perf_counter()
        with self._lock:
            self._refresh_gpu_process_map(now)
            active_program_ids: list[str] = []
            root_pids: dict[str, int] = {}
            for program in programs:
                state = self._runtime.get(program.program_id)
                if state and state.process and state.process.poll() is None:
                    active_program_ids.append(program.program_id)
                    root_pids[program.program_id] = state.process.pid
            single_active = len(active_program_ids) == 1
            pid_sets = self._build_program_pid_sets(root_pids) if os.name == "nt" and root_pids else {}
            for program in programs:
                state = self._runtime.get(program.program_id)
                if not state or not state.process:
                    continue
                proc = state.process
                if proc.poll() is not None:
                    state.cpu_percent = None
                    state._cpu_last_check = None
                    state._cpu_last_time_100ns = None
                    state.gpu_mem_mib = None
                    continue
                pid = proc.pid
                state.cpu_percent = self._sample_cpu_percent(pid, now, state)
                gpu_mem_mib = self._gpu_mem_for_program(program.program_id, pid, pid_sets)
                if single_active:
                    if gpu_mem_mib is None:
                        gpu_mem_mib = self._gpu_global_mem_mib
                state.gpu_mem_mib = gpu_mem_mib

    def get_cpu_percent(self, program_id: str) -> float | None:
        with self._lock:
            state = self._runtime.get(program_id)
            if not state:
                return None
            return state.cpu_percent

    def get_gpu_mem_mib(self, program_id: str) -> int | None:
        with self._lock:
            state = self._runtime.get(program_id)
            if not state:
                return None
            return state.gpu_mem_mib

    def _gpu_mem_for_program(self, program_id: str, root_pid: int, pid_sets: dict[str, set[int]]) -> int | None:
        pids = pid_sets.get(program_id)
        if not pids:
            return self._gpu_pid_mem_mib.get(root_pid)
        total = 0
        matched = False
        for pid in pids:
            value = self._gpu_pid_mem_mib.get(pid)
            if value is None:
                continue
            matched = True
            total += value
        if matched:
            return total
        return self._gpu_pid_mem_mib.get(root_pid)

    def _build_program_pid_sets(self, root_pids: dict[str, int]) -> dict[str, set[int]]:
        children = self._windows_process_children_map()
        if not children:
            return {pid_key: {pid_val} for pid_key, pid_val in root_pids.items()}
        result: dict[str, set[int]] = {}
        for program_id, root_pid in root_pids.items():
            result[program_id] = self._expand_descendants(root_pid, children)
        return result

    def _expand_descendants(self, root_pid: int, children: dict[int, list[int]]) -> set[int]:
        result: set[int] = set()
        stack = [root_pid]
        while stack:
            current = stack.pop()
            if current in result:
                continue
            result.add(current)
            stack.extend(children.get(current, []))
        return result

    @staticmethod
    def _windows_process_children_map() -> dict[int, list[int]]:
        if os.name != "nt":
            return {}

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            return {}
        children: dict[int, list[int]] = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            has_item = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while has_item:
                parent = int(entry.th32ParentProcessID)
                pid = int(entry.th32ProcessID)
                children.setdefault(parent, []).append(pid)
                has_item = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return children

    def _sample_cpu_percent(self, pid: int, now: float, state: RuntimeState) -> float | None:
        total_100ns = self._get_process_cpu_time_100ns(pid)
        if total_100ns is None:
            return None
        if state._cpu_last_check is None or state._cpu_last_time_100ns is None:
            state._cpu_last_check = now
            state._cpu_last_time_100ns = total_100ns
            return 0.0
        dt = max(1e-6, now - state._cpu_last_check)
        dproc = max(0, total_100ns - state._cpu_last_time_100ns)
        state._cpu_last_check = now
        state._cpu_last_time_100ns = total_100ns
        cores = os.cpu_count() or 1
        percent = (dproc / (dt * 10_000_000 * cores)) * 100.0
        if percent < 0:
            return 0.0
        if percent > 100.0:
            return min(percent, 100.0 * cores)
        return percent

    @staticmethod
    def _get_process_cpu_time_100ns(pid: int) -> int | None:
        if os.name != "nt":
            return None

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            return None
        try:
            creation = ctypes.c_ulonglong()
            exit_time = ctypes.c_ulonglong()
            kernel = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                return None
            return int(kernel.value + user.value)
        finally:
            kernel32.CloseHandle(handle)

    def _refresh_gpu_process_map(self, now: float) -> None:
        if now - self._gpu_last_refresh < 1.0:
            return
        self._gpu_last_refresh = now
        self._gpu_pid_mem_mib = {}
        self._gpu_global_mem_mib = None
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW
            if self._refresh_gpu_process_map_windows(creationflags):
                return
        global_cmd = ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
        try:
            global_completed = subprocess.run(
                global_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1.5,
                creationflags=creationflags,
            )
        except Exception:
            global_completed = None
        if global_completed and global_completed.returncode == 0:
            for raw_line in (global_completed.stdout or "").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 1:
                    continue
                try:
                    self._gpu_global_mem_mib = int(float(parts[0]))
                except Exception:
                    self._gpu_global_mem_mib = None
                break
        cmd = [
            "nvidia-smi",
            "--query-compute-apps=pid,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1.5,
                creationflags=creationflags,
            )
        except Exception:
            return
        if completed.returncode != 0:
            return
        for raw_line in (completed.stdout or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
                mem_mib = int(float(parts[1]))
            except Exception:
                continue
            if pid > 0:
                self._gpu_pid_mem_mib[pid] = mem_mib

    def _refresh_gpu_process_map_windows(self, creationflags: int) -> bool:
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance -Namespace root\\cimv2 -ClassName Win32_PerfFormattedData_GPUPerformanceCounters_GPUProcessMemory "
            "| Select-Object Name,DedicatedUsage "
            "| ConvertTo-Json -Compress",
        ]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1.5,
                creationflags=creationflags,
            )
        except Exception:
            return False
        if completed.returncode != 0:
            return False
        raw = (completed.stdout or "").strip()
        if not raw:
            return False
        try:
            payload = json.loads(raw)
        except Exception:
            return False
        rows = payload if isinstance(payload, list) else [payload]
        pid_to_bytes: dict[int, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("Name") or "")
            match = re.search(r"pid_(\d+)", name)
            if not match:
                continue
            try:
                pid = int(match.group(1))
            except Exception:
                continue
            value = row.get("DedicatedUsage")
            try:
                usage_bytes = int(float(value))
            except Exception:
                continue
            if pid <= 0 or usage_bytes < 0:
                continue
            pid_to_bytes[pid] = pid_to_bytes.get(pid, 0) + usage_bytes
        if not pid_to_bytes:
            return False
        self._gpu_pid_mem_mib = {pid: int(round(raw_bytes / (1024 * 1024))) for pid, raw_bytes in pid_to_bytes.items()}
        return True

    def _append_log(self, program: ProgramEntry, event: str, detail: str = "") -> None:
        self._log_seq += 1
        self._logs.append(
            LogEntry(
                seq=self._log_seq,
                ts=time.time(),
                program_id=program.program_id,
                program_name=program.display_name,
                event=event,
                detail=detail,
            )
        )

    def _build_command(self, program: ProgramEntry) -> list[str]:
        path = Path(program.path)
        suffix = path.suffix.lower()
        args = self._parse_args(program.args)
        runtime = program.interpreter.strip()
        if suffix in {".py", ".pyw"}:
            interpreter = runtime or "python"
            return [interpreter, str(path), *args]
        if suffix in {".js", ".mjs", ".cjs"}:
            interpreter = runtime or "node"
            return [interpreter, str(path), *args]
        if suffix in {".ts", ".tsx"}:
            interpreter = runtime or "ts-node"
            return [interpreter, str(path), *args]
        if suffix == ".jar":
            interpreter = runtime or "java"
            return [interpreter, "-jar", str(path), *args]
        if suffix == ".dll":
            interpreter = runtime or "dotnet"
            return [interpreter, str(path), *args]
        if suffix in {".ps1"}:
            interpreter = runtime or "powershell.exe"
            return [
                interpreter,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(path),
                *args,
            ]
        if suffix in {".rb"}:
            interpreter = runtime or "ruby"
            return [interpreter, str(path), *args]
        if suffix in {".php"}:
            interpreter = runtime or "php"
            return [interpreter, str(path), *args]
        if suffix in {".pl"}:
            interpreter = runtime or "perl"
            return [interpreter, str(path), *args]
        if suffix in {".lua"}:
            interpreter = runtime or "lua"
            return [interpreter, str(path), *args]
        if suffix in {".r"}:
            interpreter = runtime or "Rscript"
            return [interpreter, str(path), *args]
        if suffix in {".vbs"}:
            interpreter = runtime or "cscript.exe"
            return [interpreter, "//nologo", str(path), *args]
        if suffix in {".sh"}:
            interpreter = runtime or "bash"
            return [interpreter, str(path), *args]
        if runtime:
            return [runtime, str(path), *args]
        if os.name == "nt" and suffix in {".bat", ".cmd"}:
            return ["cmd.exe", "/c", str(path), *args]
        return [str(path), *args]

    def _read_output(self, program_id: str, proc: subprocess.Popen) -> None:
        try:
            stream = proc.stdout
            if not stream:
                return
            for raw_line in stream:
                text = self._decode_output_line(raw_line).rstrip("\r\n")
                with self._lock:
                    state = self._runtime.get(program_id)
                    if not state or state.process is not proc:
                        continue
                    state.output_seq += 1
                    state.output.append((state.output_seq, text))
                    if "Traceback (most recent call last):" in text:
                        state.traceback_pending = True
                    if state.traceback_pending:
                        if (":" in text) and ("Error" in text or "Exception" in text):
                            state.runtime_error_detected = True
                            state.runtime_error_message = text.strip()
                            state.traceback_pending = False
                    elif "Unhandled exception" in text:
                        state.runtime_error_detected = True
                        state.runtime_error_message = text.strip()
        except Exception as exc:
            with self._lock:
                state = self._runtime.get(program_id)
                if state:
                    state.output_seq += 1
                    state.output.append((state.output_seq, f"[输出读取异常] {exc}"))
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass

    @staticmethod
    def _decode_output_line(raw_line: bytes | str) -> str:
        if isinstance(raw_line, str):
            return raw_line
        preferred = locale.getpreferredencoding(False) or "gbk"
        encodings: list[str] = []
        for item in ("utf-8", "utf-8-sig", "gb18030", preferred):
            key = item.lower()
            if key not in {e.lower() for e in encodings}:
                encodings.append(item)
        for enc in encodings:
            try:
                return raw_line.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw_line.decode(preferred, errors="replace")

    @staticmethod
    def _parse_args(raw: str) -> list[str]:
        value = raw.strip()
        if not value:
            return []
        try:
            return shlex.split(value, posix=False)
        except ValueError:
            return [value]

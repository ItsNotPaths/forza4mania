"""Tk app entry point + tab orchestration.

Three tabs: Convert (the actual workflow), Settings (paths + modes),
and Log (a streaming view of what the worker thread is doing). Long jobs
run on a background thread; UI polls a queue.Queue every ~100ms for log
lines and progress updates.

Everything written to the log is also tee'd to ``forzamania.log`` next to
the .exe — Steam/Proton swallows stdout/stderr, so without that file
crashes are invisible. The file logger also catches uncaught exceptions
from any thread and writes a final stderr-redirect line so subprocess
chatter from Blender / NadeoImporter shows up too.
"""
from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import ttk

from settings import Settings
from ui.convert_tab import ConvertTab
from ui.log_tab import LogTab
from ui.settings_tab import SettingsTab


def _log_path() -> Path:
    """forzamania.log lives next to the .exe (or the dev script)."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent.parent
    return base / "forzamania.log"


class _Tee:
    """Mirror writes to multiple sinks. Used to send stdout/stderr to both
    the original stream (for terminal-launched dev runs) and our log file
    (for Steam-launched builds where the original stream is /dev/null)."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for stream in self._streams:
            try:
                stream.write(s)
                stream.flush()
            except Exception:
                pass

    def flush(self):
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:
                pass


class App:
    """Top-level Tk window + state shared across tabs.

    Tabs read settings from `self.settings` and stream log lines via
    `self.log_queue.put(...)`. The main loop drains the queue periodically
    and forwards lines to the LogTab.
    """

    def __init__(self) -> None:
        self._open_log_file()
        self._install_global_excepthooks()

        self.root = tk.Tk()
        self.root.title("forzamania — FM4 → TM2020")
        self.root.geometry("960x640")

        self.settings = Settings.load()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)

        self.log_tab = LogTab(nb)
        self.convert_tab = ConvertTab(nb, app=self)
        self.settings_tab = SettingsTab(nb, app=self)

        nb.add(self.convert_tab.frame, text="Convert")
        nb.add(self.settings_tab.frame, text="Settings")
        nb.add(self.log_tab.frame, text="Log")

        self.log(f"[boot] forzamania started, log: {_log_path()}")
        self.log(f"[boot] frozen={getattr(sys, 'frozen', False)} platform={sys.platform}")

        self._drain_log()

    # ---- file logging -----------------------------------------------

    def _open_log_file(self) -> None:
        """Open forzamania.log for append + redirect stdout/stderr to it.

        Tee'd: writes still go to the original streams when run from a
        terminal, but under Steam/Proton (where original is /dev/null) the
        file is the only place we'll see anything.
        """
        path = _log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fp = open(path, "a", encoding="utf-8", buffering=1)
            self._log_fp.write(
                f"\n=========== {datetime.now().isoformat()} session start ===========\n"
            )
            self._log_fp.flush()
        except OSError:
            self._log_fp = None
            return

        # Also capture print()/traceback chatter
        sys.stdout = _Tee(sys.stdout, self._log_fp)
        sys.stderr = _Tee(sys.stderr, self._log_fp)

    def _install_global_excepthooks(self) -> None:
        """Make sure crashes from any thread land in the log file too."""
        def hook(exc_type, exc, tb):
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            if self._log_fp is not None:
                self._log_fp.write(f"[!] uncaught: {text}\n")
                self._log_fp.flush()
            sys.__stderr__.write(text)

        sys.excepthook = hook
        if hasattr(threading, "excepthook"):
            threading.excepthook = lambda args: hook(args.exc_type, args.exc_value, args.exc_traceback)

    # ---- log queue --------------------------------------------------

    def log(self, line: str) -> None:
        """Thread-safe log line. Worker threads call this; UI + file capture it."""
        self.log_queue.put(line)
        if getattr(self, "_log_fp", None) is not None:
            try:
                self._log_fp.write(line.rstrip() + "\n")
                self._log_fp.flush()
            except Exception:
                pass

    def run_in_worker(self, target, *args, **kwargs) -> None:
        """Spawn a worker thread for one long-running job. Refuses if a
        worker is already running so we don't pile concurrent runs."""
        if self.worker is not None and self.worker.is_alive():
            self.log("[!] another job is still running; wait for it to finish")
            return

        def wrapped():
            try:
                target(*args, **kwargs)
            except Exception as e:
                self.log(f"[!] worker crashed: {type(e).__name__}: {e}")
                self.log(traceback.format_exc())

        self.worker = threading.Thread(target=wrapped, daemon=True)
        self.worker.start()

    def _drain_log(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_tab.append(line)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    def mainloop(self) -> int:
        self.root.mainloop()
        return 0


def run_app() -> int:
    return App().mainloop()

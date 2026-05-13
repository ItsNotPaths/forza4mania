"""Tk app entry point + tab orchestration.

Three tabs: Convert (the actual workflow), Settings (paths + modes),
and Log (a streaming view of what the worker thread is doing). Long jobs
run on a background thread; UI polls a queue.Queue every ~100ms for log
lines and progress updates.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from settings import Settings
from ui.convert_tab import ConvertTab
from ui.log_tab import LogTab
from ui.settings_tab import SettingsTab


class App:
    """Top-level Tk window + state shared across tabs.

    Tabs read settings from `self.settings` and stream log lines via
    `self.log_queue.put(...)`. The main loop drains the queue periodically
    and forwards lines to the LogTab.
    """

    def __init__(self) -> None:
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

        self._drain_log()

    def log(self, line: str) -> None:
        """Thread-safe log line. Worker threads call this; UI shows it."""
        self.log_queue.put(line)

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
                import traceback
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

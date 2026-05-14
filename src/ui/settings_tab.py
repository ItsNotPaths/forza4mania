"""Settings tab — paths, modes, download buttons."""
from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

from external_downloader import (
    download_blendermania_dotnet,
    download_nadeo_importer,
)

if TYPE_CHECKING:
    from ui.app import App


def _exe_dir() -> Path:
    """Where forzamania.exe (or the dev script) actually lives.

    Under PyInstaller --onefile, ``sys.executable`` is the .exe — its parent
    is the user's chosen install dir. (Avoid sys._MEIPASS: that's the temp
    extraction dir which gets wiped on exit.)
    In dev mode, fall back to the repo root via __file__.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


_FIELDS = [
    ("FM4 Media dir",          "fm4_install_dir",          "dir"),
    ("Blender executable",     "blender_path",             "file"),
    ("TM2020 install dir",     "tm_install_dir",           "dir"),
    ("TM2020 user dir",        "tm_user_dir",              "dir"),
    ("NadeoImporter.exe",      "nadeo_importer_path",      "file"),
    ("Blendermania_Dotnet",    "blendermania_dotnet_path", "file"),
]


class SettingsTab:
    def __init__(self, parent: ttk.Notebook, app: "App") -> None:
        self.app = app
        self.frame = ttk.Frame(parent)
        self.vars: dict[str, tk.StringVar] = {}

        # Path fields
        for row, (label, key, kind) in enumerate(_FIELDS):
            ttk.Label(self.frame, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
            v = tk.StringVar(value=str(getattr(self.app.settings, key) or ""))
            self.vars[key] = v
            ttk.Entry(self.frame, textvariable=v, width=70).grid(
                row=row, column=1, sticky="we", padx=4, pady=4
            )
            ttk.Button(
                self.frame, text="Browse...",
                command=lambda k=key, knd=kind: self._browse(k, knd),
            ).grid(row=row, column=2, padx=4, pady=4)

        next_row = len(_FIELDS)

        # Linux mode toggle
        self.linux_var = tk.BooleanVar(value=self.app.settings.linux_mode)
        ttk.Checkbutton(
            self.frame,
            text="Linux mode (rewrite paths to Z:\\… for Wine — best-effort, may not be enough)",
            variable=self.linux_var,
        ).grid(row=next_row, column=0, columnspan=3, sticky="w", padx=8, pady=8)
        next_row += 1

        # Download buttons
        actions = ttk.Frame(self.frame)
        actions.grid(row=next_row, column=0, columnspan=3, sticky="w", padx=8, pady=8)
        ttk.Button(
            actions, text="Download NadeoImporter",
            command=self._download_nadeo,
        ).pack(side="left", padx=4)
        ttk.Button(
            actions, text="Download Blendermania_Dotnet",
            command=self._download_dotnet,
        ).pack(side="left", padx=4)
        ttk.Button(
            actions, text="Save settings",
            command=self._save,
        ).pack(side="left", padx=24)

        self.frame.columnconfigure(1, weight=1)

    # ---- handlers --------------------------------------------------

    def _browse(self, key: str, kind: str) -> None:
        if kind == "dir":
            path = filedialog.askdirectory(title=key, initialdir=self.vars[key].get() or "/")
        else:
            path = filedialog.askopenfilename(title=key, initialdir=self.vars[key].get() or "/")
        if path:
            self.vars[key].set(path)

    def _commit_to_settings(self) -> None:
        for key, var in self.vars.items():
            setattr(self.app.settings, key, var.get())
        self.app.settings.linux_mode = self.linux_var.get()

    def _save(self) -> None:
        self._commit_to_settings()
        path = self.app.settings.save()
        self.app.log(f"[settings] saved to {path}")

    def _tools_dir(self) -> Path:
        """Where downloaded helpers (NadeoImporter, dotnet) get extracted.

        Lives next to forzamania.exe under ``./tools/``. Two reasons over
        dropping into the TM install dir:
          - Steam protects steamapps/, our process may not have write access.
          - Keeps everything our app installs/manages in one place that's
            obvious to find and easy to clean up.
        """
        return _exe_dir() / "tools"

    def _download_nadeo(self) -> None:
        dst = self._tools_dir()
        self.app.log(f"[download] NadeoImporter → {dst} ...")

        def work():
            res = download_nadeo_importer(dst, progress=self._progress("NadeoImporter"))
            self.app.log(f"[download] done: {len(res.extracted_files)} files in {res.dst_dir}")
            for p in res.extracted_files:
                if p.name.lower().endswith(".exe"):
                    self.vars["nadeo_importer_path"].set(str(p))
                    self.app.log(f"[download] auto-set nadeo_importer_path = {p}")
                    break

        threading.Thread(target=work, daemon=True).start()

    def _download_dotnet(self) -> None:
        dst = self._tools_dir()
        self.app.log(f"[download] Blendermania_Dotnet → {dst} ...")

        def work():
            res = download_blendermania_dotnet(dst, progress=self._progress("Blendermania_Dotnet"))
            self.app.log(f"[download] done: {len(res.extracted_files)} files in {res.dst_dir}")
            for p in res.extracted_files:
                if p.name.lower().endswith(".exe"):
                    self.vars["blendermania_dotnet_path"].set(str(p))
                    self.app.log(f"[download] auto-set blendermania_dotnet_path = {p}")
                    break

        threading.Thread(target=work, daemon=True).start()

    def _progress(self, label: str):
        last = [0]
        def cb(done: int, total: int) -> None:
            if total <= 0:
                return
            pct = int(done * 100 / total)
            if pct - last[0] >= 10:
                self.app.log(f"[download] {label}: {pct}%")
                last[0] = pct
        return cb

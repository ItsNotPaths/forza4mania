"""Append-only log view. Worker threads write to it via App.log()."""
from __future__ import annotations

import tkinter as tk
from tkinter import scrolledtext, ttk


class LogTab:
    def __init__(self, parent: ttk.Notebook) -> None:
        self.frame = ttk.Frame(parent)
        self.text = scrolledtext.ScrolledText(
            self.frame, wrap="word", state="disabled",
            font=("monospace", 10),
        )
        self.text.pack(fill="both", expand=True)

    def append(self, line: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", line.rstrip() + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

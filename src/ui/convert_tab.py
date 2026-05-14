"""Convert tab — auto-list FM4 tracks, check the ones to port, hit Export All."""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ui.app import App


def _bundle_root() -> Path:
    """Repo root in dev, sys._MEIPASS in PyInstaller --onefile mode.

    Use this for read-only data shipped *inside* the bundle (vendored Forza
    parsers, the Blender export script). Don't use it for user-droppable
    files — those should live next to the .exe via _exe_dir().
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent.parent


def _exe_dir() -> Path:
    """Where forzamania.exe (or the dev script) actually lives.

    Use this for files the user might drop in themselves (the seed map,
    downloaded helpers under tools/). In dev, falls back to the repo root.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def _find_seed_map() -> Path | None:
    """Locate empty_stadium.Map.Gbx — user-droppable next to .exe wins, then
    fall back to whatever was baked into the bundle."""
    for candidate in (
        _exe_dir() / "assets" / "empty_stadium.Map.Gbx",
        _bundle_root() / "assets" / "empty_stadium.Map.Gbx",
    ):
        if candidate.is_file():
            return candidate
    return None


def _pick_first_ribbon(track_dir: Path) -> Path | None:
    """Auto-select a ribbon for one-shot conversion. Lowest-numbered Ribbon_NN
    wins (Ribbon_00 = the canonical / forward configuration on every FM4
    track we've checked)."""
    ribbons = sorted(
        p for p in track_dir.iterdir()
        if p.is_dir() and p.name.lower().startswith("ribbon")
    )
    return ribbons[0] if ribbons else None


def _enumerate_tracks(fm4_media_dir: Path) -> list[Path]:
    """Find every FM4 track folder under <media>/tracks/.

    A "track" = a subdir of tracks/ that contains a bin.zip AND at least one
    Ribbon_NN subfolder. Filters out partial / non-track directories.
    """
    tracks_root = fm4_media_dir / "tracks"
    if not tracks_root.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(tracks_root.iterdir()):
        if not p.is_dir():
            continue
        if not (p / "bin.zip").is_file():
            continue
        if _pick_first_ribbon(p) is None:
            continue
        out.append(p)
    return out


class ConvertTab:
    def __init__(self, parent: ttk.Notebook, app: "App") -> None:
        self.app = app
        self.frame = ttk.Frame(parent)
        self.track_vars: dict[str, tk.BooleanVar] = {}
        self.track_paths: dict[str, Path] = {}

        # --- top row: source dir + refresh -----------------------------
        top = ttk.Frame(self.frame)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="FM4 source").pack(side="left")
        self.source_var = tk.StringVar(value=self.app.settings.fm4_install_dir)
        ttk.Entry(top, textvariable=self.source_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="Browse...", command=self._pick_source).pack(side="left", padx=2)
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="left", padx=2)

        # --- track list (scrollable Checkbutton column) ----------------
        list_frame = ttk.LabelFrame(self.frame, text="Tracks")
        list_frame.pack(fill="both", expand=True, padx=8, pady=4)

        self.canvas = tk.Canvas(list_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.list_inner = ttk.Frame(self.canvas)
        self.list_inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.create_window((0, 0), window=self.list_inner, anchor="nw")
        # Mouse wheel scrolling on the canvas (Linux uses Button-4/5)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(+1, "units"))

        # --- bottom row: select-helpers + export -----------------------
        helpers = ttk.Frame(self.frame)
        helpers.pack(fill="x", padx=8, pady=(4, 0))
        ttk.Button(helpers, text="Select all", command=self._select_all).pack(side="left", padx=2)
        ttk.Button(helpers, text="Select none", command=self._select_none).pack(side="left", padx=2)
        self.stop_at_fbx = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            helpers, text="Stop at FBX (skip NadeoImporter + map compose)",
            variable=self.stop_at_fbx,
        ).pack(side="left", padx=12)

        actions = ttk.Frame(self.frame)
        actions.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Button(
            actions, text="Export checked",
            command=self._export_checked,
        ).pack(side="left", padx=2)
        ttk.Button(
            actions, text="Export all",
            command=self._export_all,
        ).pack(side="left", padx=2)

        # --- status ----------------------------------------------------
        self.status_var = tk.StringVar(value="ready")
        ttk.Label(self.frame, textvariable=self.status_var, foreground="#666").pack(
            anchor="w", padx=8, pady=(0, 8)
        )

        self.refresh()

    # ---- handlers --------------------------------------------------

    def _on_mousewheel(self, event):
        # Windows/Mac wheel; Linux uses Button-4/5 bound separately
        delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta, "units")

    def _pick_source(self) -> None:
        initial = self.source_var.get() or str(Path.home())
        path = filedialog.askdirectory(title="Pick FM4 Media folder", initialdir=initial)
        if path:
            self.source_var.set(path)
            self.refresh()

    def refresh(self) -> None:
        """Re-scan the source dir and rebuild the checkbox list."""
        for child in self.list_inner.winfo_children():
            child.destroy()
        self.track_vars.clear()
        self.track_paths.clear()

        src = self.source_var.get().strip()
        if not src:
            ttk.Label(self.list_inner, text="set FM4 source above").pack(anchor="w", padx=8, pady=4)
            return

        tracks = _enumerate_tracks(Path(src))
        if not tracks:
            ttk.Label(
                self.list_inner,
                text=f"no tracks found under {Path(src) / 'tracks'}",
            ).pack(anchor="w", padx=8, pady=4)
            return

        for t in tracks:
            ribbon = _pick_first_ribbon(t)
            label = f"{t.name}  ({ribbon.name if ribbon else 'no ribbons'})"
            var = tk.BooleanVar(value=False)
            self.track_vars[t.name] = var
            self.track_paths[t.name] = t
            ttk.Checkbutton(self.list_inner, text=label, variable=var).pack(
                anchor="w", padx=8, pady=1
            )

        self._set_status(f"{len(tracks)} tracks found")

    def _select_all(self) -> None:
        for v in self.track_vars.values():
            v.set(True)

    def _select_none(self) -> None:
        for v in self.track_vars.values():
            v.set(False)

    def _set_status(self, s: str) -> None:
        self.status_var.set(s)
        self.app.log(f"[ui] {s}")

    def _export_checked(self) -> None:
        checked = [name for name, v in self.track_vars.items() if v.get()]
        if not checked:
            messagebox.showinfo("forzamania", "No tracks checked.")
            return
        self._export(checked)

    def _export_all(self) -> None:
        if not self.track_vars:
            messagebox.showinfo("forzamania", "No tracks loaded. Set the FM4 source first.")
            return
        self._export(list(self.track_vars.keys()))

    def _export(self, track_names: list[str]) -> None:
        # Snapshot stop-at-fbx so background thread doesn't race with UI changes.
        stop_at_fbx = self.stop_at_fbx.get()
        self._set_status(f"queued {len(track_names)} track(s)")
        self.app.run_in_worker(self._batch, track_names, stop_at_fbx)

    # ---- worker (runs on background thread) -----------------------

    def _batch(self, track_names: list[str], stop_at_fbx: bool) -> None:
        for i, name in enumerate(track_names, 1):
            track_dir = self.track_paths.get(name)
            if track_dir is None:
                self.app.log(f"[batch] {name}: track path lost; skipping")
                continue
            ribbon = _pick_first_ribbon(track_dir)
            if ribbon is None:
                self.app.log(f"[batch] {name}: no ribbon dir; skipping")
                continue
            self.app.log(f"\n=== [{i}/{len(track_names)}] {name} ({ribbon.name}) ===")
            try:
                self._pipeline(track_dir, ribbon.name, stop_at_fbx)
            except Exception as e:
                import traceback
                self.app.log(f"[!] {name} failed: {type(e).__name__}: {e}")
                self.app.log(traceback.format_exc())
        self._set_status(f"batch done: {len(track_names)} track(s)")

    def _pipeline(self, track_dir: Path, ribbon: str, stop_at_fbx: bool) -> None:
        # Heavy imports stay inside the worker so a UI launch doesn't pay the
        # numpy + Forza-X360-IO bpy-stub cost just to render the Tk window.
        from blender_bridge import dump_chunk, export_chunk_to_fbx, find_blender
        from chunker import chunk_track
        from fm4 import read_track
        from materials import map_material
        from textures import extract_track_textures
        from xml_writers import write_item_xml, write_mesh_params

        ribbon_dir = track_dir / ribbon
        out_root = Path(self.app.settings.tm_user_dir or str(Path.home() / "forzamania-out"))
        items_root = out_root / "Items" / "Forzamania" / track_dir.name
        textures_root = items_root / "_Textures"
        items_root.mkdir(parents=True, exist_ok=True)

        log = self.app.log
        log(f"[1/6] reading track: {track_dir}/{ribbon}")
        ir = read_track(track_dir, ribbon_dir)
        log(f"      meshes={len(ir.meshes)} instances={len(ir.instances)} textures={len(ir.textures)}")

        log("[2/6] chunking...")
        chunks = chunk_track(
            ir,
            tile_size_m=self.app.settings.tile_size_m,
            tri_budget=self.app.settings.tri_budget,
        )
        chunks = [c for c in chunks if c.tri_count > 0]
        log(f"      {len(chunks)} chunks (max tri={max((c.tri_count for c in chunks), default=0)})")

        log(f"[3/6] extracting textures → {textures_root}")
        tex_paths = extract_track_textures(ir, textures_root)
        log(f"      {len(tex_paths)} dds files")

        log(f"[4/6] FBX + XML for {len(chunks)} chunks → {items_root}")
        try:
            blender = find_blender(
                Path(self.app.settings.blender_path) if self.app.settings.blender_path else None
            )
        except FileNotFoundError as e:
            log(f"[!] {e}")
            self._set_status("blender not found")
            return
        log(f"      blender: {blender}")

        export_script = _bundle_root() / "scripts" / "blender_export.py"
        fbx_paths: list[Path] = []
        for i, chunk in enumerate(chunks, 1):
            log(f"      [{i}/{len(chunks)}] {chunk.name}  tri={chunk.tri_count}")
            mats = {}
            for mk in chunk.mesh_keys:
                mesh = ir.meshes[mk]
                for j, fm4_mat in enumerate(mesh.materials):
                    mats[(mk, j)] = map_material(fm4_mat, chunk.name, j, tex_paths, items_root)

            fbx_path = items_root / f"{chunk.name}.fbx"
            json_path = items_root / f"{chunk.name}.chunk.json"
            dump_chunk(chunk, ir, mats, fbx_path, json_path)
            try:
                export_chunk_to_fbx(json_path, blender, export_script, timeout=900.0)
            except Exception as e:
                log(f"      [!] export failed: {type(e).__name__}: {e}")
                # Full traceback for the FIRST failure of the run only — past
                # that, the cause is almost certainly the same and we don't
                # want to spam the log with 119 identical stacks.
                if not getattr(self, "_logged_first_export_traceback", False):
                    import traceback
                    log("      ---- traceback ----")
                    for tb_line in traceback.format_exc().splitlines():
                        log(f"      {tb_line}")
                    log("      ---- end traceback ----")
                    self._logged_first_export_traceback = True
                continue
            fbx_paths.append(fbx_path)

            # Dedup by name — TM2020Material isn't hashable (dataclass default
            # with eq=True), and material_name is the natural identity anyway.
            seen: set[str] = set()
            tm_mats = []
            for m in mats.values():
                if m.name in seen:
                    continue
                seen.add(m.name)
                tm_mats.append(m)
            tm_mats.sort(key=lambda x: x.name)
            write_mesh_params(fbx_path, tm_mats)
            write_item_xml(fbx_path)

        if stop_at_fbx:
            log(f"[done] stopped at FBX. {len(fbx_paths)} chunks ready in {items_root}")
            return

        # ---- NadeoImporter step --------------------------------------
        log("[5/6] running NadeoImporter on each chunk...")
        from nadeo_runner import (
            convert_chunk,
            find_nadeo_importer,
        )

        try:
            importer = find_nadeo_importer(
                Path(self.app.settings.nadeo_importer_path) if self.app.settings.nadeo_importer_path else None,
                tm_install_dir=Path(self.app.settings.tm_install_dir) if self.app.settings.tm_install_dir else None,
            )
        except FileNotFoundError as e:
            log(f"[!] {e}  — pipeline stops at FBX. Use Settings → Download NadeoImporter.")
            return
        log(f"      importer: {importer}")

        item_gbx_paths: list[Path] = []
        wine_cmd = self.app.settings.wine_command if self.app.settings.linux_mode else None
        for fbx in fbx_paths:
            mesh_res, item_res = convert_chunk(importer, fbx, self.app.settings.linux_mode, wine_cmd)
            if not mesh_res.ok:
                log(f"      [!] {fbx.name} mesh step failed: {mesh_res.stderr.strip()[:200]}")
                continue
            if not item_res.ok:
                log(f"      [!] {fbx.name} item step failed: {item_res.stderr.strip()[:200]}")
                continue
            item_gbx = fbx.with_suffix(".Item.Gbx")
            if item_gbx.is_file():
                item_gbx_paths.append(item_gbx)

        log(f"      {len(item_gbx_paths)} of {len(fbx_paths)} items converted")

        if not item_gbx_paths:
            log("[!] no items converted; skipping map composition")
            return

        # ---- Map composer step ---------------------------------------
        log("[6/6] composing .Map.Gbx...")
        from dotnet_runner import find_blendermania_dotnet
        from map_composer import chunk_to_placed_item, compose_map

        try:
            dotnet = find_blendermania_dotnet(
                Path(self.app.settings.blendermania_dotnet_path) if self.app.settings.blendermania_dotnet_path else None,
                tm_install_dir=Path(self.app.settings.tm_install_dir) if self.app.settings.tm_install_dir else None,
            )
        except FileNotFoundError as e:
            log(f"[!] {e}  — items still usable in TM editor. Use Settings → Download Blendermania_Dotnet.")
            return

        seed_map = _find_seed_map()
        if seed_map is None:
            log(
                "[!] no seed map found. Drop a blank empty_stadium.Map.Gbx into "
                "an 'assets/' folder next to forzamania.exe and retry. "
                "(Items are still produced; only the .Map.Gbx step is skipped.)"
            )
            return
        log(f"      seed map: {seed_map}")

        maps_root = out_root / "Maps" / "Forzamania"
        maps_root.mkdir(parents=True, exist_ok=True)
        out_map = maps_root / f"{track_dir.name}.Map.Gbx"

        placed = []
        chunks_by_name = {c.name: c for c in chunks}
        for gbx in item_gbx_paths:
            chunk_name = gbx.stem.replace(".Item", "")
            chunk = chunks_by_name.get(chunk_name)
            if chunk is None:
                continue
            placed.append(chunk_to_placed_item(chunk, gbx))

        result = compose_map(
            dotnet, seed_map, out_map, placed,
            block_name="StadiumPlatform",
            linux_mode=self.app.settings.linux_mode,
            wine_cmd=wine_cmd,
        )
        if result.ok:
            log(f"[done] map written: {out_map}")
        else:
            log(f"[!] map compose failed ({result.explanation}): {result.stderr.strip()[:300]}")

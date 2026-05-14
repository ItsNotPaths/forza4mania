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

    def _resolve_user_dir_from_nadeo_ini(self) -> Path | None:
        """Parse Nadeo.ini's UserDir and expand the {userdocs} token.

        This guarantees our intermediate FBX/XML paths land in the same
        physical dir NadeoImporter resolves them against — sidestepping
        all the Wine-path-mangling pitfalls of trusting our own setting.
        """
        tm_install = self.app.settings.tm_install_dir
        if not tm_install:
            return None
        ini_path = Path(tm_install) / "Nadeo.ini"
        if not ini_path.is_file():
            return None
        try:
            for line in ini_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line.lower().startswith("userdir"):
                    continue
                _, _, raw = line.partition("=")
                raw = raw.strip()
                # {userdocs} → user's "My Documents". On Wine that's
                # C:\users\steamuser\Documents (a Wine-side path that
                # both we and NadeoImporter address the same way).
                if "{userdocs}" in raw:
                    if sys.platform == "win32":
                        raw = raw.replace("{userdocs}", "C:\\users\\steamuser\\Documents")
                    else:
                        raw = raw.replace("{userdocs}", str(Path.home() / "Documents"))
                resolved = Path(raw.replace("\\", "/"))
                if resolved.is_dir():
                    return resolved
                # Fall back to the Wine prefix variant if a Windows-style
                # path doesn't exist on Linux (dev mode).
                return resolved
        except OSError:
            pass
        return None

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

        # Resolve out_root by reading Nadeo.ini if available — that's the
        # ONE source NadeoImporter agrees with. Trusting our own settings
        # field has bitten us with stale in-memory values + Wine path
        # mangling. Nadeo.ini's UserDir uses {userdocs} which Wine
        # resolves to C:\users\steamuser\Documents (the canonical location
        # both we and NadeoImporter can write to and read from).
        out_root = self._resolve_user_dir_from_nadeo_ini() or Path(
            self.app.settings.tm_user_dir or str(Path.home() / "forzamania-out")
        )
        self.app.log(f"      using TM user dir: {out_root}")

        # Intermediate: FBX + XML + textures live inside <userdir>/Work/...
        # because NadeoImporter resolves its path argument relative to that
        # Work/ dir. Final .Item.Gbx gets moved to <userdir>/Items/... after
        # NadeoImporter runs (which is where TM2020 looks for them in the
        # in-game item editor).
        work_root = out_root / "Work"
        work_items_root = work_root / "Items" / "Forzamania" / track_dir.name
        items_root = out_root / "Items" / "Forzamania" / track_dir.name
        textures_root = work_items_root / "_Textures"
        work_items_root.mkdir(parents=True, exist_ok=True)
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

        log(f"[4/6] FBX + XML for {len(chunks)} chunks → {work_items_root}")
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
                    mats[(mk, j)] = map_material(fm4_mat, chunk.name, j, tex_paths, work_items_root)

            fbx_path = work_items_root / f"{chunk.name}.fbx"
            json_path = work_items_root / f"{chunk.name}.chunk.json"
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

        # Sanity: NadeoImporterMaterialLib.txt MUST live next to the .exe.
        # Without it NadeoImporter fails silently on every material — same
        # symptom as a generic mesh-step failure with no stderr output.
        matlib = importer.parent / "NadeoImporterMaterialLib.txt"
        if not matlib.is_file():
            log(
                f"[!] missing {matlib} — NadeoImporter needs this file (it ships in the same zip "
                "as NadeoImporter.exe). Without it every chunk silently fails. "
                "Re-extract the full Nadeo zip into tools/ and retry."
            )

        # Sanity: Nadeo.ini must also live next to NadeoImporter.exe, OR
        # NadeoImporter aborts at init with "ini file not found". The
        # canonical copy lives in the TM2020 install root; mirror it into
        # our tools/ dir if missing.
        ini = importer.parent / "Nadeo.ini"
        if not ini.is_file():
            tm_install = self.app.settings.tm_install_dir
            src_ini = Path(tm_install) / "Nadeo.ini" if tm_install else None
            if src_ini and src_ini.is_file():
                try:
                    ini.write_bytes(src_ini.read_bytes())
                    log(f"      copied Nadeo.ini from {src_ini} → {ini}")
                except OSError as e:
                    log(f"[!] failed to copy Nadeo.ini: {e}")
            else:
                log(
                    f"[!] missing {ini} — NadeoImporter aborts without it. "
                    f"Copy {tm_install or '<TM install>'}/Nadeo.ini into tools/ "
                    "(or set TM2020 install dir in Settings so we can auto-copy)."
                )

        item_gbx_paths: list[Path] = []
        wine_cmd = self.app.settings.wine_command if self.app.settings.linux_mode else None

        # Keep the per-chunk log line short, but on the FIRST failure dump
        # the full output (stdout AND stderr, plus return code) so we don't
        # have to guess what NadeoImporter is upset about. NadeoImporter
        # writes most diagnostics to stdout on Windows, not stderr.
        first_failure_logged = False

        def _log_full(label: str, fbx_name: str, res) -> None:
            nonlocal first_failure_logged
            log(f"      [!] {fbx_name} {label} failed (rc={res.returncode}): {(res.stderr or res.stdout).strip()[:200]}")
            if not first_failure_logged:
                log(f"      ---- {label} stdout ----")
                for line in (res.stdout or "").splitlines()[:40]:
                    log(f"      {line}")
                log(f"      ---- {label} stderr ----")
                for line in (res.stderr or "").splitlines()[:40]:
                    log(f"      {line}")
                log(f"      ---- end ----")
                first_failure_logged = True

        for fbx in fbx_paths:
            mesh_res, item_res = convert_chunk(
                importer, fbx, self.app.settings.linux_mode, wine_cmd,
                work_root=work_root,
            )
            if not mesh_res.ok:
                _log_full("mesh step", fbx.name, mesh_res)
                continue
            if not item_res.ok:
                _log_full("item step", fbx.name, item_res)
                continue
            # NadeoImporter writes the .Item.Gbx (and .Mesh.Gbx, .Shape.Gbx)
            # next to the FBX inside Work/. TM2020's in-game item editor
            # only sees what's inside <userdir>/Items/, so move the final
            # GBXs there. We keep the .fbx + xml in Work/ as the audit trail.
            item_gbx_in_work = fbx.with_suffix(".Item.Gbx")
            if not item_gbx_in_work.is_file():
                continue
            item_gbx_dst = items_root / item_gbx_in_work.name
            try:
                if item_gbx_dst.exists():
                    item_gbx_dst.unlink()
                item_gbx_in_work.rename(item_gbx_dst)
            except OSError as e:
                log(f"      [!] move {item_gbx_in_work.name} → Items/ failed: {e}")
                continue
            item_gbx_paths.append(item_gbx_dst)

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

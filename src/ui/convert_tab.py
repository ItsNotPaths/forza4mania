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
    """Locate any blank ``*.Map.Gbx`` to use as the map composer's seed.

    Search order: ``<exe>/assets/`` (user-droppable wins) then the bundled
    ``<MEIPASS>/assets/``. Any file matching ``*.Map.Gbx`` is acceptable —
    users can drop in whatever blank stadium map they exported from TM2020;
    the dotnet helper just opens-and-modifies the file we hand it.
    """
    for d in (_exe_dir() / "assets", _bundle_root() / "assets"):
        if not d.is_dir():
            continue
        # Prefer a file literally named empty_stadium.Map.Gbx if present;
        # otherwise pick the first .Map.Gbx alphabetically (deterministic).
        canonical = d / "empty_stadium.Map.Gbx"
        if canonical.is_file():
            return canonical
        for candidate in sorted(d.glob("*.Map.Gbx")):
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
        """Resolve the TM2020 user dir, preferring a cross-prefix Wine path.

        Cross-prefix problem: when forzamania.exe runs under Proton in its
        own compatdata prefix, ``C:\\users\\steamuser\\Documents`` resolves
        to that prefix's Documents — NOT to the TM2020 prefix where TM
        actually reads its Maps/Items from. Writing there means TM never
        sees our output.

        Strategy:
          1. Look at ``tm_install_dir`` and walk up to ``steamapps/``.
          2. Scan ``steamapps/compatdata/*/pfx/drive_c/users/steamuser/
             Documents/Trackmania`` for a real TM dir (must contain
             ``Maps/`` to be the right one).
          3. Express that as a ``Z:\\...`` path — Wine's Z: drive maps to
             Linux ``/``, so the path is addressable from inside any
             prefix (including our own forzamania prefix).
          4. Fall back to Nadeo.ini's ``{userdocs}`` expansion if no
             cross-prefix candidate is found.
        """
        tm_install = self.app.settings.tm_install_dir
        if not tm_install:
            return None

        # Normalize tm_install to a form whose .is_dir() will work under
        # the current process's filesystem view. Three input shapes we see:
        #   "Z:\run\media\paths\..."     — already addressable on Wine
        #   "\run\media\paths\..."       — Wine treats as path on current
        #                                  drive (usually C:); .is_dir()
        #                                  fails. Prepend Z: so it works.
        #   "/run/media/paths/..."       — Linux native, fine on Linux.
        normalized = tm_install.replace("\\", "/")
        if sys.platform == "win32" and normalized.startswith("/"):
            # Wine: bare absolute Linux path → Z: drive
            normalized = "Z:" + normalized
        tm_path = Path(normalized)

        # For path arithmetic (parents, name) we need the drive prefix
        # OFF so the components include "steamapps" etc. as named parents.
        arith_str = normalized
        if arith_str[:2].lower() == "z:":
            arith_str = arith_str[2:]
        arith_path = Path(arith_str)

        # Find the steamapps root
        for ancestor in arith_path.parents:
            if ancestor.name == "steamapps":
                # Re-attach Z: prefix for is_dir() if we're on Wine
                compatdata = ancestor / "compatdata"
                if sys.platform == "win32":
                    compatdata = Path("Z:" + str(compatdata))
                if compatdata.is_dir():
                    # Pick the TM2020 prefix that has a real Maps/ dir.
                    # Most-recently-modified wins as a tiebreaker.
                    candidates = []
                    for entry in compatdata.iterdir():
                        if not entry.is_dir():
                            continue
                        tm_user = (entry / "pfx" / "drive_c" / "users" /
                                   "steamuser" / "Documents" / "Trackmania")
                        if (tm_user / "Maps").is_dir():
                            candidates.append((tm_user.stat().st_mtime, tm_user))
                    if candidates:
                        candidates.sort(reverse=True)
                        chosen = candidates[0][1]
                        # On Wine, return as a Z:\ path so it's addressable
                        # from inside whatever prefix forzamania.exe lives in.
                        # `chosen` already has the Z: prefix because
                        # `compatdata` did — Path concatenation preserves it.
                        return chosen
                break

        # Step 4: Nadeo.ini fallback (single-prefix case, dev mode, etc.)
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
                if "{userdocs}" in raw:
                    if sys.platform == "win32":
                        raw = raw.replace("{userdocs}", "C:\\users\\steamuser\\Documents")
                    else:
                        raw = raw.replace("{userdocs}", str(Path.home() / "Documents"))
                resolved = Path(raw.replace("\\", "/"))
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

        # Two user dirs in play under Proton:
        #   out_root     = forzamania's own Wine prefix view of Documents/
        #                  Trackmania (always C:\users\steamuser\Documents\
        #                  Trackmania). NadeoImporter runs in this prefix
        #                  and only addresses files via {userdir}/Work/, so
        #                  the FBX/XML/Item.Gbx pipeline MUST live here.
        #   tm_user_dir  = TM2020's own prefix view of Documents/Trackmania
        #                  (cross-prefix, addressed via Z:\). The .Map.Gbx
        #                  + .Item.Gbx files have to land here at the end
        #                  for TM to actually see them.
        # We do all work in out_root, then copy outputs to tm_user_dir.
        if sys.platform == "win32":
            out_root = Path("C:/users/steamuser/Documents/Trackmania")
        else:
            out_root = Path(
                self.app.settings.tm_user_dir or str(Path.home() / "forzamania-out")
            )
        tm_user_dir = self._resolve_user_dir_from_nadeo_ini()
        self.app.log(f"      working dir: {out_root}")
        if tm_user_dir is not None and tm_user_dir != out_root:
            self.app.log(f"      will copy outputs to TM2020 prefix: {tm_user_dir}")

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
                # 180s per chunk — generous for ~50k-tri FBX serialisation.
                # The hang risk from lightmap_pack is gone (we now use a
                # synthetic grid layout in _add_lightmap_uv), so the floor
                # is just Blender startup + FBX writing.
                export_chunk_to_fbx(json_path, blender, export_script, timeout=180.0)
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

            # NadeoImporter writes outputs into <userdir>/Items/<same path>/,
            # NOT next to the FBX in Work/, AND uses lowercase extensions
            # (.item.gbx etc.). Look in BOTH locations and accept either
            # case so we work with both old and new NadeoImporter behavior.
            stem = fbx.stem
            stem_only = stem
            search_dirs = [items_root, fbx.parent]
            item_gbx_found: Path | None = None
            for d in search_dirs:
                for name in (f"{stem_only}.Item.Gbx", f"{stem_only}.item.gbx",
                             f"{stem_only}.Item.gbx"):
                    candidate = d / name
                    if candidate.is_file():
                        item_gbx_found = candidate
                        break
                if item_gbx_found is not None:
                    break

            if item_gbx_found is None:
                # Mesh/Item rc=0 but no output file exists anywhere we know
                # to look. Log the search paths so future debugging isn't blind.
                log(
                    f"      [!] {fbx.name}: rc=0 from both steps but no .Item.Gbx in "
                    f"{[str(d) for d in search_dirs]}"
                )
                continue

            # Normalize to canonical PascalCase + ensure it lives in items_root.
            item_gbx_dst = items_root / f"{stem_only}.Item.Gbx"
            if item_gbx_found != item_gbx_dst:
                try:
                    if item_gbx_dst.exists():
                        item_gbx_dst.unlink()
                    item_gbx_found.rename(item_gbx_dst)
                except OSError as e:
                    log(f"      [!] move {item_gbx_found} → {item_gbx_dst} failed: {e}")
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

        import json as _json
        placed = []
        chunks_by_name = {c.name: c for c in chunks}
        for gbx in item_gbx_paths:
            chunk_name = gbx.stem.replace(".Item", "")
            chunk = chunks_by_name.get(chunk_name)
            if chunk is None:
                continue
            # TM2020 looks items up by their path relative to <userdir>/Items/,
            # forward-slashed, with the .Item.Gbx extension. Our items live at
            # <userdir>/Items/Forzamania/<track>/<chunk>.Item.Gbx.
            items_rel = f"Forzamania/{track_dir.name}/{gbx.name}"
            # blender_export re-centred the item mesh on its bbox centre and
            # wrote that centre (RAW Blender world coords) to <chunk>.center.json
            # next to the FBX. chunk_to_placed_item applies the addon's
            # Blender->TM Position convention so the item lands correctly.
            center_path = work_items_root / f"{chunk_name}.center.json"
            try:
                center = tuple(_json.loads(center_path.read_text())["center"])
            except (OSError, ValueError, KeyError):
                log(f"      [!] {chunk_name}: no center sidecar, placing at origin")
                center = (0.0, 0.0, 0.0)
            placed.append(chunk_to_placed_item(gbx, items_rel, center))

        result = compose_map(
            dotnet, seed_map, out_map, placed,
            block_name="StadiumPlatform",
            linux_mode=self.app.settings.linux_mode,
            wine_cmd=wine_cmd,
        )
        if result.ok:
            log(f"[done] map written: {out_map}")
        else:
            # Like NadeoImporter, the dotnet helper writes most of its
            # diagnostics to stdout, not stderr. Show both.
            log(f"[!] map compose failed ({result.explanation}, rc={result.returncode})")
            log(f"      ---- compose stdout ----")
            for line in (result.stdout or "").splitlines()[:40]:
                log(f"      {line}")
            log(f"      ---- compose stderr ----")
            for line in (result.stderr or "").splitlines()[:40]:
                log(f"      {line}")
            log(f"      ---- end ----")
            return

        # ---- Cross-prefix copy ---------------------------------------
        # Under Proton, forzamania.exe runs in its own Wine prefix while
        # TM2020 runs in a different one. NadeoImporter + dotnet produced
        # files in OUR prefix's Documents/Trackmania/{Items,Maps}/. TM2020
        # never sees them unless we copy them into ITS prefix.
        # tm_user_dir was resolved earlier via cross-prefix scan (Z:\ form).
        if tm_user_dir is not None and tm_user_dir != out_root:
            self._copy_outputs_to_tm_prefix(
                items_root, item_gbx_paths,
                out_map, tm_user_dir, track_dir.name,
            )

    def _copy_outputs_to_tm_prefix(
        self,
        items_root: Path,
        item_gbx_paths: list[Path],
        out_map: Path,
        tm_user_dir: Path,
        track_name: str,
    ) -> None:
        """Mirror our Items/Forzamania/<track>/ + Maps/Forzamania/<track>.Map.Gbx
        from forzamania's prefix into TM2020's prefix."""
        log = self.app.log
        log("[7/7] copying outputs to TM2020 prefix...")

        dst_items = tm_user_dir / "Items" / "Forzamania" / track_name
        dst_maps = tm_user_dir / "Maps" / "Forzamania"
        try:
            dst_items.mkdir(parents=True, exist_ok=True)
            dst_maps.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log(f"[!] mkdir on TM prefix failed: {e}")
            return

        # Normalize destination filenames to PascalCase (.Item.Gbx,
        # .Mesh.Gbx, .Shape.Gbx). NadeoImporter emits inconsistent casing
        # under Wine — observed: ".Item.gbx" (capital I, lowercase g).
        # The map composer references ".Item.Gbx" exactly, and TM2020's
        # filesystem lookup is case-sensitive on Linux ext4 — any mismatch
        # = "missing item". Match the suffix CASE-INSENSITIVELY (lowercase
        # the name for comparison) so every casing variant normalizes.
        CANON = {".item.gbx": ".Item.Gbx",
                 ".mesh.gbx": ".Mesh.Gbx",
                 ".shape.gbx": ".Shape.Gbx"}
        copied = 0
        for src in items_root.iterdir():
            if not src.is_file():
                continue
            low = src.name.lower()
            normalized_name = None
            for suf, canon in CANON.items():
                if low.endswith(suf):
                    normalized_name = src.name[: -len(suf)] + canon
                    break
            if normalized_name is None:
                continue  # not a gbx output file
            dst = dst_items / normalized_name
            try:
                dst.write_bytes(src.read_bytes())
                copied += 1
            except OSError as e:
                log(f"[!] copy {src.name} failed: {e}")

        try:
            dst_map = dst_maps / out_map.name
            dst_map.write_bytes(out_map.read_bytes())
            log(f"      copied {copied} item files + map → {tm_user_dir}")
            log(f"      open in TM2020: My Local Maps → Forzamania → {track_name}")
        except OSError as e:
            log(f"[!] copy {out_map.name} failed: {e}")

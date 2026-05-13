"""Convert tab — pick a track + ribbon, hit Go, watch the Log tab."""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ui.app import App


def _bundle_root() -> Path:
    """Repo root in dev, sys._MEIPASS in PyInstaller --onefile mode."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent.parent


class ConvertTab:
    def __init__(self, parent: ttk.Notebook, app: "App") -> None:
        self.app = app
        self.frame = ttk.Frame(parent)

        self.track_var = tk.StringVar(value="")
        self.ribbon_var = tk.StringVar(value="")

        # Track folder picker
        ttk.Label(self.frame, text="Track folder").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(self.frame, textvariable=self.track_var, width=70).grid(
            row=0, column=1, sticky="we", padx=4, pady=6
        )
        ttk.Button(self.frame, text="Browse...", command=self._pick_track).grid(
            row=0, column=2, padx=4, pady=6
        )

        # Ribbon dropdown — populated when a track is chosen
        ttk.Label(self.frame, text="Ribbon").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.ribbon_combo = ttk.Combobox(self.frame, textvariable=self.ribbon_var, width=68, state="readonly")
        self.ribbon_combo.grid(row=1, column=1, sticky="we", padx=4, pady=6)

        # Action buttons
        actions = ttk.Frame(self.frame)
        actions.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=12)
        ttk.Button(actions, text="Convert (full pipeline)", command=self._convert).pack(side="left", padx=4)
        ttk.Button(actions, text="Stop at FBX", command=self._convert_to_fbx).pack(side="left", padx=4)

        # Status line
        self.status_var = tk.StringVar(value="ready")
        ttk.Label(self.frame, textvariable=self.status_var, foreground="#666").grid(
            row=3, column=0, columnspan=3, sticky="w", padx=8, pady=4
        )

        self.frame.columnconfigure(1, weight=1)

    def _set_status(self, s: str) -> None:
        self.status_var.set(s)
        self.app.log(f"[ui] {s}")

    def _pick_track(self) -> None:
        initial = self.app.settings.fm4_install_dir or str(Path.home())
        path = filedialog.askdirectory(title="Pick FM4 track folder", initialdir=initial)
        if not path:
            return
        self.track_var.set(path)
        ribbons = sorted(p.name for p in Path(path).iterdir() if p.is_dir() and p.name.lower().startswith("ribbon"))
        self.ribbon_combo["values"] = ribbons
        if ribbons:
            self.ribbon_combo.set(ribbons[0])

    def _convert_to_fbx(self) -> None:
        self._run_pipeline(stop_at_fbx=True)

    def _convert(self) -> None:
        self._run_pipeline(stop_at_fbx=False)

    def _run_pipeline(self, stop_at_fbx: bool) -> None:
        track_dir = self.track_var.get().strip()
        ribbon = self.ribbon_var.get().strip()
        if not track_dir or not ribbon:
            messagebox.showerror("forzamania", "Pick a track folder and a ribbon.")
            return

        self._set_status("running...")
        self.app.run_in_worker(self._pipeline, Path(track_dir), ribbon, stop_at_fbx)

    def _pipeline(self, track_dir: Path, ribbon: str, stop_at_fbx: bool) -> None:
        # All the heavy imports happen here so a UI launch doesn't pay the
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
                log(f"      [!] export failed: {e}")
                continue
            fbx_paths.append(fbx_path)

            tm_mats = sorted({m for m in mats.values()}, key=lambda x: x.name)
            write_mesh_params(fbx_path, tm_mats)
            write_item_xml(fbx_path)

        if stop_at_fbx:
            log(f"[done] stopped at FBX. {len(fbx_paths)} chunks ready in {items_root}")
            self._set_status(f"done (FBX only): {len(fbx_paths)} chunks")
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
            self._set_status("NadeoImporter missing")
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
            self._set_status("no items produced")
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
            log(f"[!] {e}  — items are still usable in TM editor. Use Settings → Download Blendermania_Dotnet.")
            self._set_status(".Item.Gbx ready, no map composed")
            return

        seed_map = _bundle_root() / "assets" / "empty_stadium.Map.Gbx"
        if not seed_map.is_file():
            log(f"[!] missing seed map at {seed_map} — can't compose. Add one and retry.")
            self._set_status("seed map missing")
            return

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
            self._set_status(f"done: {out_map.name}")
        else:
            log(f"[!] map compose failed ({result.explanation}): {result.stderr.strip()[:300]}")
            self._set_status("map compose failed")

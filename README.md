# forzamania

Ports **Forza Motorsport 4** tracks to **Trackmania 2020** maps.

A small Nim + [wayluigi](https://github.com/ItsNotPaths/wayluigi) GUI app
orchestrates a native pipeline: read FM4 geometry → spatially chunk → export
FBX via headless Blender → import to `.Item.Gbx` and compose a `.Map.Gbx` with
nadeo-freeporter, then copy into TM2020's (Proton) user dir.

## Build

```sh
./download-deps.sh                 # once: vendor wayluigi / rawk-luigi / libmspack / Forza-X360-IO
./release.sh --local               # GUI-only (fast) — bundles freeporter
./release.sh --local --with-tools  # full bundle — also builds lzxd_helper + x360io
```

Output lands in `../forzamania-release/`. Backends: `--x11` (default), `--wayland`,
`--windows` (mingw cross). The shared build logic is `scripts/build_release.sh`
(also driven by the GitHub `release.yml` matrix).

## Runtime helpers (spawned CLIs, bundled under `tools/`)

- **x360io** — FM4 geometry reader (Python/Nuitka onefile; `patches/x360io_cli.py`). The only numpy in the system.
- **lzxd_helper** — method-21 `bin.zip` LZX decompressor (C / libmspack; `scripts/lzxd_helper.c`).
- **nadeo-freeporter** — FBX → `.Item.Gbx` + `.Map.Gbx` composer (Nim; [tm2020-freeporter](https://github.com/ItsNotPaths/tm2020-freeporter)). Auto-downloaded, or Settings → Download freeporter.
- **Blender** — external, headless FBX export (`scripts/blender_export.py`, runs inside Blender).

The app (`app/*.nim`) does orchestration + plain-array coordinate math; all heavy
parsing lives in the spawned CLIs.

Thanks to:
- austinbaccus — Forza-X360-IO — https://github.com/austinbaccus
- Doliman100 — Forza-X360-IO — https://github.com/Doliman100
- skyslide22 — TM2020 Blender addon — https://github.com/skyslide22

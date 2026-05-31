## Persistent app settings (paths + conversion knobs), backed by a JSON file.
##
## Nim port of src/settings.py. JSON keys are kept snake_case so the file at
## ~/.config/forzamania/settings.json (Linux) / %APPDATA%/forzamania (Windows)
## is interchangeable with the Python reference build during the pivot.
##
## The dead Wine fields (linux_mode, wine_command) are intentionally dropped —
## the Nim app is native on every target.
import std/[json, os, strutils, sets]
when defined(windows): import std/algorithm  # sort() — only used by the Windows path

type Settings* = object
  fm4InstallDir*:     string   ## FM4 Media dir (…/33E7B39F/Media)
  blenderPath*:       string
  tmInstallDir*:      string   ## where Trackmania.exe lives
  tmUserDir*:         string   ## Documents/Trackmania (Items/ + Maps/ go here)
  nadeoImporterPath*: string   ## freeporter override (importer + map composer)
  x360ioPath*:        string   ## x360io FM4-reader CLI override
  tileSizeM*:         float
  triBudget*:         int
  defaultSurfaceLink*: string
  defaultPhysicsId*:  string

proc defaultSettings*(): Settings =
  Settings(
    tileSizeM: 64.0,
    triBudget: 50_000,
    defaultSurfaceLink: "PlatformTech",
    defaultPhysicsId: "Asphalt",
  )

# ---- paths ---------------------------------------------------------------

proc configDir(): string =
  when defined(windows):
    let appdata = getEnv("APPDATA")
    if appdata.len > 0: return appdata / "forzamania"
  let xdg = getEnv("XDG_CONFIG_HOME")
  if xdg.len > 0: return xdg / "forzamania"
  result = getHomeDir() / ".config" / "forzamania"

proc settingsPath*(): string = configDir() / "settings.json"

# ---- autodetect heuristics (port of the _detect_* helpers) ---------------

const fm4Guesses = [
  "/run/media/paths/SSS-Games/xenia_canary_windows/content/0000000000000000/4D530910/00007000/33E7B39F/Media",
]

proc firstExisting(cands: openArray[string]): string =
  for c in cands:
    let p = expandTilde(c)
    if fileExists(p) or dirExists(p): return p
  return ""

# --- Steam library discovery (shared; Blender ships as Steam app 365670) ---

proc steamRoots(): seq[string] =
  ## Candidate Steam base dirs (each may hold steamapps/libraryfolders.vdf).
  when defined(windows):
    @[getEnv("ProgramFiles(x86)", r"C:\Program Files (x86)") / "Steam",
      getEnv("ProgramFiles",      r"C:\Program Files") / "Steam"]
  else:
    let home = getHomeDir()
    @[home / ".steam/steam",
      home / ".steam/root",
      home / ".local/share/Steam",
      home / ".var/app/com.valvesoftware.Steam/data/Steam"]  # flatpak Steam

proc steamLibraries*(): seq[string] =
  ## Every Steam library dir: the roots plus each `path` entry in
  ## steamapps/libraryfolders.vdf (so secondary drives / custom libraries are
  ## found, not just the default install). Deduped.
  var seen = initHashSet[string]()
  for root in steamRoots():
    if not dirExists(root): continue
    if root notin seen:
      seen.incl root; result.add root
    let vdf = root / "steamapps" / "libraryfolders.vdf"
    if not fileExists(vdf): continue
    # entries look like:  "path"   "/run/media/.../SteamLibrary"
    for line in readFile(vdf).splitLines():
      let s = line.strip()
      if not s.startsWith("\"path\""): continue
      let parts = s.split('"')
      if parts.len < 4: continue
      let p = parts[^2].replace("\\\\", "\\")  # VDF doubles backslashes on Windows
      if p.len > 0 and p notin seen and dirExists(p):
        seen.incl p; result.add p

proc windowsBlenderInstalls(): seq[string] =
  ## Program Files\Blender Foundation\Blender X.Y\blender.exe, newest first.
  when defined(windows):
    for pf in [getEnv("ProgramFiles",      r"C:\Program Files"),
               getEnv("ProgramFiles(x86)", r"C:\Program Files (x86)")]:
      let bf = pf / "Blender Foundation"
      if not dirExists(bf): continue
      var found: seq[string]
      for kind, p in walkDir(bf):
        if kind == pcDir and fileExists(p / "blender.exe"):
          found.add p / "blender.exe"
      found.sort(order = Descending)   # "Blender 5.1" before "Blender 4.5"
      result.add found

proc detectBlender(): string =
  ## Cross-platform: Steam libraries (incl. custom drives) → Windows Program
  ## Files (newest) → Unix standard dirs → PATH.
  let exe = when defined(windows): "blender.exe" else: "blender"
  var cands: seq[string]
  for lib in steamLibraries():
    cands.add lib / "steamapps" / "common" / "Blender" / exe
  cands.add windowsBlenderInstalls()
  when not defined(windows):
    let home = getHomeDir()
    cands.add @["/usr/bin/blender", "/usr/local/bin/blender",
                "/opt/blender/blender", "/snap/bin/blender",
                home / ".local/share/flatpak/exports/bin/org.blender.Blender",
                "/var/lib/flatpak/exports/bin/org.blender.Blender"]
  for c in cands:
    if fileExists(c): return c
  findExe("blender")   # PATH fallback (distro symlinks, user installs)

proc detectTmInstall(): string =
  ## Trackmania = Steam app 2225540 → <lib>/steamapps/common/Trackmania, across
  ## all Steam libraries. Falls back to the Ubisoft Connect install on Windows.
  ## (Manual override in Settings always wins — autodetect only fills blanks.)
  for lib in steamLibraries():
    let d = lib / "steamapps" / "common" / "Trackmania"
    if dirExists(d): return d
  when defined(windows):
    for pf in [getEnv("ProgramFiles(x86)", r"C:\Program Files (x86)"),
               getEnv("ProgramFiles",      r"C:\Program Files")]:
      let d = pf / "Ubisoft" / "Ubisoft Game Launcher" / "games" / "Trackmania"
      if dirExists(d): return d
  return ""

proc detectTmUserDir(tmInstall: string): string =
  ## Documents/Trackmania — on Linux/Proton it's inside the Steam compatdata
  ## prefix, not $HOME. Mirrors the Python _detect_tm_user_dir scan.
  var cands = @[getHomeDir() / "Documents" / "Trackmania"]
  let idx = tmInstall.find("steamapps")
  if idx > 0:
    let steamRoot = tmInstall[0 ..< idx]
    for appId in ["2225540", "2225070"]:
      cands.add(steamRoot / "steamapps" / "compatdata" / appId / "pfx" /
                "drive_c" / "users" / "steamuser" / "Documents" / "Trackmania")
  for c in cands:
    if dirExists(c): return c
  return ""

proc autodetect*(s: var Settings) =
  ## Best-effort probe; only fills BLANK fields (never overwrites a user value).
  if s.blenderPath.len == 0:   s.blenderPath = detectBlender()
  if s.tmInstallDir.len == 0:  s.tmInstallDir = detectTmInstall()
  if s.tmUserDir.len == 0 and s.tmInstallDir.len > 0:
    s.tmUserDir = detectTmUserDir(s.tmInstallDir)
  if s.fm4InstallDir.len == 0: s.fm4InstallDir = firstExisting(fm4Guesses)

# ---- load / save ---------------------------------------------------------

proc load*(): Settings =
  result = defaultSettings()
  let p = settingsPath()
  if not fileExists(p):
    autodetect(result)
    return
  var node: JsonNode
  try:
    node = parseJson(readFile(p))
  except CatchableError:
    return  # corrupt file → bare defaults (matches the Python fallback)
  if node.kind != JObject: return
  template str(key: string, field: untyped) =
    if node.hasKey(key): field = node[key].getStr(field)
  str("fm4_install_dir",     result.fm4InstallDir)
  str("blender_path",        result.blenderPath)
  str("tm_install_dir",      result.tmInstallDir)
  str("tm_user_dir",         result.tmUserDir)
  str("nadeo_importer_path", result.nadeoImporterPath)
  str("x360io_path",         result.x360ioPath)
  str("default_surface_link", result.defaultSurfaceLink)
  str("default_physics_id",  result.defaultPhysicsId)
  if node.hasKey("tile_size_m"): result.tileSizeM = node["tile_size_m"].getFloat(result.tileSizeM)
  if node.hasKey("tri_budget"):  result.triBudget = node["tri_budget"].getInt(result.triBudget)

proc save*(s: Settings) =
  createDir(configDir())
  let node = %* {
    "fm4_install_dir":     s.fm4InstallDir,
    "blender_path":        s.blenderPath,
    "tm_install_dir":      s.tmInstallDir,
    "tm_user_dir":         s.tmUserDir,
    "nadeo_importer_path": s.nadeoImporterPath,
    "x360io_path":         s.x360ioPath,
    "tile_size_m":         s.tileSizeM,
    "tri_budget":          s.triBudget,
    "default_surface_link": s.defaultSurfaceLink,
    "default_physics_id":  s.defaultPhysicsId,
  }
  writeFile(settingsPath(), pretty(node, 2))

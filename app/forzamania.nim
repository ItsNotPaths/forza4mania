## forzamania — FM4 → TM2020 track porter (Nim/wayluigi rewrite).
##
## Phase 1: the 3-tab GUI shell (Convert / Settings / Log). Settings is fully
## wired (load/edit/save/autodetect against ~/.config/forzamania/settings.json);
## Convert is a stub until the pipeline is ported. The Python src/ stays the
## reference oracle until parity.

import std/[strutils, os]
import rawk_luigi
import settings
import tracks
import pipeline
import external_downloader

# ---- shared app state ----------------------------------------------------
# luigi invokes button/checkbox callbacks as plain C function pointers, so the
# widgets they touch live in module globals (the same pattern luigi's own C
# apps use). Single window, single thread — no synchronisation needed yet.

type SettingsFields = object
  fm4, blender, tmInstall, tmUser, importer, x360io: ptr Textbox
  tileSize, triBudget, surfaceLink, physicsId: ptr Textbox

var
  gSettings: Settings
  gFields: SettingsFields
  gSettingsStatus: ptr Label
  gDownloadBtn: ptr Button    # Settings → Download freeporter (wired in main)
  gLog: ptr Code

# Export runs on a worker thread (luigi isn't thread-safe). The worker streams
# log lines through a channel; the UI thread drains it — woken by a thread-safe
# windowPostMessage on X11/Windows, or by an animation tick on Wayland (where
# post is a no-op). One job at a time.
type ExportInput = object
  tracks*:    seq[TrackInfo]
  stopAtFbx*: bool
  cfg*:       Settings

var
  gWindow: ptr Window
  gLogChan: Channel[string]
  gWorker: Thread[ExportInput]
  gJobRunning: bool
  gDlWorker: Thread[string]    # freeporter download (Settings button)
  gDlRunning: bool

const
  JOB_DONE = "\x01__forzamania_job_done__"  # export-batch channel sentinel
  # Download sentinel is a PREFIX: the worker appends the resulting binary path
  # (passed via the channel, not a string global — globals aren't GC-safe across
  # threads). Empty path = download failed.
  DL_DONE  = "\x01__forzamania_dl_done__\x01"

# ---- helpers -------------------------------------------------------------

proc textboxStr(tb: ptr Textbox): string =
  ## Read a textbox's contents. luigi stores the buffer realloc'd to exactly
  ## `bytes` and does NOT NUL-terminate it, so copy by length rather than $cstr.
  if tb == nil or tb.text.isNil or tb.bytes <= 0: return ""
  result = newString(tb.bytes)
  copyMem(addr result[0], tb.text, tb.bytes)

proc setText(tb: ptr Textbox; s: string) =
  textboxClear(tb, false)
  if s.len > 0:
    textboxReplace(tb, s.cstring, s.len, false)
  elementRefresh(addr tb.e)

proc logLine(line: string) =
  ## Append one line to the Log tab. (Phase 2 will route worker-thread output
  ## here via a channel; for now everything runs on the UI thread.)
  if gLog != nil:
    codeInsertContent(gLog, (line & "\n").cstring, -1, false)
    elementRefresh(addr gLog.e)
  echo line

# ---- Settings tab --------------------------------------------------------

proc addRow(parent: ptr Element; label, value: string): ptr Textbox =
  let row = panelCreate(parent, PANEL_HORIZONTAL or ELEMENT_H_FILL)
  discard labelCreate(addr row.e, 0, label.cstring, -1)
  result = textboxCreate(addr row.e, ELEMENT_H_FILL)
  if value.len > 0:
    textboxReplace(result, value.cstring, value.len, false)

# Right-pad every label to a fixed width so all the textboxes line up. luigi's
# built-in bitmap font (we build -d:luigiNoFreetype) is fixed-width, so equal
# character counts mean equal pixel widths. Longest label is
# "Importer (freeporter):" (22 chars); 24 leaves a 2-space gap before the box.
const kLabelWidth = 24
proc padLabel(name: string): string = alignLeft(name & ":", kLabelWidth)

proc onSettingsSave(cp: pointer) {.cdecl.} =
  gSettings.fm4InstallDir     = textboxStr(gFields.fm4)
  gSettings.blenderPath       = textboxStr(gFields.blender)
  gSettings.tmInstallDir      = textboxStr(gFields.tmInstall)
  gSettings.tmUserDir         = textboxStr(gFields.tmUser)
  gSettings.nadeoImporterPath = textboxStr(gFields.importer)
  gSettings.x360ioPath        = textboxStr(gFields.x360io)
  gSettings.defaultSurfaceLink = textboxStr(gFields.surfaceLink)
  gSettings.defaultPhysicsId  = textboxStr(gFields.physicsId)
  try:    gSettings.tileSizeM = parseFloat(textboxStr(gFields.tileSize).strip())
  except ValueError: logLine("[settings] tile size not a number; keeping " & $gSettings.tileSizeM)
  try:    gSettings.triBudget = parseInt(textboxStr(gFields.triBudget).strip())
  except ValueError: logLine("[settings] tri budget not an integer; keeping " & $gSettings.triBudget)

  try:
    save(gSettings)
    let msg = "saved → " & settingsPath()
    if gSettingsStatus != nil:
      labelSetContent(gSettingsStatus, msg.cstring, msg.len)
      elementRefresh(addr gSettingsStatus.e)
    logLine("[settings] " & msg)
  except CatchableError as e:
    logLine("[settings] save failed: " & e.msg)

proc onSettingsAutodetect(cp: pointer) {.cdecl.} =
  autodetect(gSettings)  # only fills blank fields
  setText(gFields.fm4,       gSettings.fm4InstallDir)
  setText(gFields.blender,   gSettings.blenderPath)
  setText(gFields.tmInstall, gSettings.tmInstallDir)
  setText(gFields.tmUser,    gSettings.tmUserDir)
  setText(gFields.importer,  gSettings.nadeoImporterPath)
  setText(gFields.x360io,    gSettings.x360ioPath)
  logLine("[settings] re-autodetected (blank fields only)")

proc buildSettingsTab(tab: ptr Element) =
  let p = panelCreate(tab, PANEL_GRAY or PANEL_MEDIUM_SPACING or PANEL_SCROLL or
                           ELEMENT_V_FILL or ELEMENT_H_FILL)
  discard labelCreate(addr p.e, 0, "Paths", -1)
  gFields.fm4       = addRow(addr p.e, padLabel("FM4 source"),       gSettings.fm4InstallDir)
  gFields.blender   = addRow(addr p.e, padLabel("Blender"),          gSettings.blenderPath)
  gFields.tmInstall = addRow(addr p.e, padLabel("TM2020 install"),   gSettings.tmInstallDir)
  gFields.tmUser    = addRow(addr p.e, padLabel("TM2020 user dir"),  gSettings.tmUserDir)
  gFields.importer  = addRow(addr p.e, padLabel("Importer (freeporter)"), gSettings.nadeoImporterPath)
  gFields.x360io    = addRow(addr p.e, padLabel("x360io (FM4 reader)"),   gSettings.x360ioPath)

  let autoBtn = buttonCreate(addr p.e, 0, "autodetect", -1)
  autoBtn.invoke = onSettingsAutodetect
  # Download freeporter into <appdir>/tools/ (.invoke wired in main, since the
  # download worker is defined later in this module).
  gDownloadBtn = buttonCreate(addr p.e, 0, "Download freeporter", -1)

  discard spacerCreate(addr p.e, SPACER_LINE or ELEMENT_H_FILL, 0, 1)
  discard labelCreate(addr p.e, 0, "Conversion knobs", -1)
  gFields.tileSize    = addRow(addr p.e, padLabel("Tile size (m)"),    $gSettings.tileSizeM)
  gFields.triBudget   = addRow(addr p.e, padLabel("Tri budget"),       $gSettings.triBudget)
  gFields.surfaceLink = addRow(addr p.e, padLabel("Default surface link"), gSettings.defaultSurfaceLink)
  gFields.physicsId   = addRow(addr p.e, padLabel("Default physics id"),   gSettings.defaultPhysicsId)

  discard spacerCreate(addr p.e, SPACER_LINE or ELEMENT_H_FILL, 0, 1)
  let saveBtn = buttonCreate(addr p.e, 0, "Save", -1)
  saveBtn.invoke = onSettingsSave
  gSettingsStatus = labelCreate(addr p.e, 0, "loaded", -1)

# ---- Convert tab ---------------------------------------------------------

type TrackRow = object
  info: TrackInfo
  box:  ptr Checkbox

var
  gTracks: seq[TrackRow]
  gTrackList: ptr Panel       # scroll panel holding one checkbox per track
  gStopAtFbx: ptr Checkbox
  gConvertStatus: ptr Label

proc setConvertStatus(s: string) =
  if gConvertStatus != nil:
    labelSetContent(gConvertStatus, s.cstring, s.len)
    elementRefresh(addr gConvertStatus.e)

proc refreshTracks() =
  if gTrackList == nil: return
  elementDestroyDescendents(addr gTrackList.e)
  gTracks.setLen(0)
  let fm4 = gSettings.fm4InstallDir
  let found = enumerateTracks(fm4)
  if found.len == 0:
    let msg = if fm4.len == 0: "set FM4 source in Settings, then Refresh"
              else: "no tracks under " & (fm4 / "tracks")
    discard labelCreate(addr gTrackList.e, 0, msg.cstring, -1)
  else:
    for t in found:
      let label = t.name & "  (" & t.ribbon & ")"
      # H_FILL so the checkbox spans the row → left-aligned (a vertical panel
      # centers non-filling children) and the whole row is clickable.
      let box = checkboxCreate(addr gTrackList.e, ELEMENT_H_FILL, label.cstring, -1)
      gTracks.add(TrackRow(info: t, box: box))
  setConvertStatus($found.len & " track(s) found")
  elementRefresh(addr gTrackList.e)

proc onRefresh(cp: pointer) {.cdecl.} = refreshTracks()

proc setAll(checked: bool) =
  for r in gTracks:
    r.box.check = (if checked: CHECK_CHECKED else: CHECK_UNCHECKED)
    elementRepaint(addr r.box.e, nil)

proc onSelectAll(cp: pointer) {.cdecl.} = setAll(true)
proc onSelectNone(cp: pointer) {.cdecl.} = setAll(false)

proc checkedTracks(): seq[TrackInfo] =
  for r in gTracks:
    if r.box.check == CHECK_CHECKED: result.add(r.info)

# ---- export worker (background thread) -----------------------------------

proc workerLog(s: string) =
  ## Called ONLY from the worker thread. Never touch luigi here — push the line
  ## through the channel and wake the UI loop (no-op on Wayland).
  gLogChan.send(s)
  if gWindow != nil: windowPostMessage(gWindow, msgUser, nil)

proc exportWorker(input: ExportInput) {.thread.} =
  workerLog("[convert] exporting " & $input.tracks.len & " track(s)" &
            (if input.stopAtFbx: " (stop at FBX)" else: ""))
  for i, t in input.tracks:
    workerLog("\n=== [" & $(i+1) & "/" & $input.tracks.len & "] " & t.name &
              " (" & t.ribbon & ") ===")
    try:
      runPipeline(t, input.cfg, input.stopAtFbx, workerLog)
    except CatchableError as e:
      workerLog("[!] " & t.name & " failed: " & e.msg)
  workerLog("[convert] batch done: " & $input.tracks.len & " track(s)")
  gLogChan.send(JOB_DONE)
  if gWindow != nil: windowPostMessage(gWindow, msgUser, nil)

proc downloadWorker(toolsDir: string) {.thread.} =
  ## Download the latest nadeo-freeporter into <toolsDir> off the UI thread. The
  ## resulting binary path travels back through the channel (DL_DONE & path); the
  ## UI drain wires it into the Importer field. Empty path = failed.
  workerLog("[downloader] fetching latest nadeo-freeporter → " & toolsDir)
  var binary = ""
  try:
    let r = downloadFreeporter(toolsDir)
    if r.binary.len > 0:
      binary = r.binary
      workerLog("[downloader] done: " & r.binary)
    else:
      workerLog("[downloader] extracted but binary not found in " & toolsDir)
  except CatchableError as e:
    workerLog("[downloader] failed: " & e.msg)
  gLogChan.send(DL_DONE & binary)
  if gWindow != nil: windowPostMessage(gWindow, msgUser, nil)

# ---- UI-thread drain -----------------------------------------------------

proc onJobDone() =
  joinThread(gWorker)          # worker has already sent JOB_DONE → returns at once
  gJobRunning = false
  when defined(wayland):
    if gWindow != nil: discard elementAnimate(addr gWindow.e, true)  # stop ticking
  setConvertStatus("done")
  when defined(fmAutoExport):
    logLine("[selftest] job complete — exiting")
    quit(0)

proc onDownloadDone(binary: string) =
  joinThread(gDlWorker)
  gDlRunning = false
  when defined(wayland):
    if gWindow != nil and not gJobRunning:
      discard elementAnimate(addr gWindow.e, true)  # stop ticking
  if binary.len > 0:
    # Auto-fill + persist the Importer path so the runner finds it next run.
    if gFields.importer != nil: setText(gFields.importer, binary)
    gSettings.nadeoImporterPath = binary
    try:
      save(gSettings)
      setConvertStatus("freeporter downloaded")
    except CatchableError as e:
      logLine("[downloader] save failed: " & e.msg)

proc drainLog() =
  while true:
    let (ok, msg) = gLogChan.tryRecv()
    if not ok: break
    if msg == JOB_DONE: onJobDone()
    elif msg.startsWith(DL_DONE): onDownloadDone(msg[DL_DONE.len .. ^1])
    else: logLine(msg)

proc onWindowMessage(e: ptr Element; m: Message; di: cint; dp: pointer): cint {.cdecl.} =
  # msgUser = woken by the worker's windowPostMessage (X11/Windows).
  # msgAnimate = the Wayland tick while a job animates the window.
  if m == msgUser or m == msgAnimate:
    drainLog()
  return 0  # don't consume — let the window's class handler run too

proc startExport(sel: seq[TrackInfo]) =
  if sel.len == 0:
    setConvertStatus("no tracks checked"); return
  if gJobRunning:
    logLine("[convert] a job is already running"); return
  gJobRunning = true
  let stop = gStopAtFbx != nil and gStopAtFbx.check == CHECK_CHECKED
  setConvertStatus("running " & $sel.len & " track(s)...")
  createThread(gWorker, exportWorker,
               ExportInput(tracks: sel, stopAtFbx: stop, cfg: gSettings))
  when defined(wayland):
    if gWindow != nil: discard elementAnimate(addr gWindow.e, false)  # start ticking

proc onExportChecked(cp: pointer) {.cdecl.} = startExport(checkedTracks())
proc onExportAll(cp: pointer) {.cdecl.} =
  setAll(true)
  startExport(checkedTracks())

proc onDownloadFreeporter(cp: pointer) {.cdecl.} =
  if gDlRunning or gJobRunning:
    logLine("[downloader] busy; try again when the current job finishes"); return
  gDlRunning = true
  let toolsDir = getAppDir() / "tools"
  setConvertStatus("downloading freeporter...")
  createThread(gDlWorker, downloadWorker, toolsDir)
  when defined(wayland):
    if gWindow != nil: discard elementAnimate(addr gWindow.e, false)

proc buildConvertTab(tab: ptr Element) =
  let p = panelCreate(tab, PANEL_GRAY or PANEL_MEDIUM_SPACING or
                           ELEMENT_V_FILL or ELEMENT_H_FILL)

  let top = panelCreate(addr p.e, PANEL_HORIZONTAL or ELEMENT_H_FILL)
  discard labelCreate(addr top.e, 0, "FM4 tracks (source set in Settings)", -1)
  discard spacerCreate(addr top.e, ELEMENT_H_FILL, 0, 0)
  let refreshBtn = buttonCreate(addr top.e, 0, "Refresh", -1)
  refreshBtn.invoke = onRefresh

  gTrackList = panelCreate(addr p.e, PANEL_SCROLL or ELEMENT_V_FILL or ELEMENT_H_FILL)

  let helpers = panelCreate(addr p.e, PANEL_HORIZONTAL or ELEMENT_H_FILL)
  (buttonCreate(addr helpers.e, 0, "Select all", -1)).invoke = onSelectAll
  (buttonCreate(addr helpers.e, 0, "Select none", -1)).invoke = onSelectNone
  gStopAtFbx = checkboxCreate(addr helpers.e, 0, "Stop at FBX", -1)

  let actions = panelCreate(addr p.e, PANEL_HORIZONTAL or ELEMENT_H_FILL)
  (buttonCreate(addr actions.e, 0, "Export checked", -1)).invoke = onExportChecked
  (buttonCreate(addr actions.e, 0, "Export all", -1)).invoke = onExportAll

  gConvertStatus = labelCreate(addr p.e, 0, "ready", -1)
  refreshTracks()

  when defined(fmAutoExport):
    # Smoke test: auto-run an export of the first 2 tracks at startup so the
    # worker→channel→post→drain log path is exercisable headlessly.
    if gTracks.len > 0:
      for i in 0 ..< min(2, gTracks.len): gTracks[i].box.check = CHECK_CHECKED
      startExport(checkedTracks())

# ---- Log tab -------------------------------------------------------------

proc buildLogTab(tab: ptr Element) =
  let p = panelCreate(tab, PANEL_EXPAND or ELEMENT_V_FILL or ELEMENT_H_FILL)
  gLog = codeCreate(addr p.e, ELEMENT_V_FILL or ELEMENT_H_FILL)

# ---- assembly ------------------------------------------------------------

proc buildUI(win: ptr Window) =
  let tabs = tabPaneCreate(addr win.e, ELEMENT_V_FILL or ELEMENT_H_FILL,
                           "Convert\tSettings\tLog")
  buildConvertTab(addr tabs.e)
  buildSettingsTab(addr tabs.e)
  buildLogTab(addr tabs.e)

proc main() =
  gSettings = load()
  gLogChan.open()
  initialise()
  let win = windowCreate(nil, 0, "forzamania — FM4 → TM2020", 960, 640)
  gWindow = win
  win.e.messageUser = onWindowMessage   # drains the worker→UI log channel
  buildUI(win)
  # The download button lives in the Settings tab (built above) but its callback
  # is defined after the download worker, so wire it here.
  if gDownloadBtn != nil: gDownloadBtn.invoke = onDownloadFreeporter
  logLine("[boot] forzamania (Nim/wayluigi) started")
  logLine("[boot] settings: " & settingsPath())
  quit messageLoop()

when isMainModule:
  main()

## Download nadeo-freeporter to the user's tools dir.
##
## Nim port of `src/external_downloader.py`. Grabs the latest GitHub release and
## picks the asset matching the platform forzamania RUNS AS (freeporter is a
## child process in the same OS-ABI environment): windows→.exe, else→ELF. The
## native Nim build needs HTTPS, so compile this with `-d:ssl` (the Settings
## "Download" button is the only caller; manually pointing the Importer field at
## an existing binary is the offline fallback).
##
## Standard PKZIP (deflate) extraction is delegated to the system `unzip`/`tar`
## — binzip.nim is FM4 method-21-specific and can't inflate. This is a
## click-time convenience, so a system-tool dependency is acceptable here.

import std/[json, os, osproc, strutils, httpclient]

const
  FREEPORTER_REPO = "ItsNotPaths/tm2020-freeporter"
  FREEPORTER_LATEST_API = "https://api.github.com/repos/" & FREEPORTER_REPO & "/releases/latest"

type
  DownloadResult* = object
    name*:    string
    url*:     string
    dstDir*:  string
    binary*:  string   ## path to the extracted platform binary ("" if not found)

proc freeporterAssetForPlatform*(): (string, string) =
  ## (asset_keyword, binary_filename) for the running platform.
  when defined(windows): ("windows", "nadeo-freeporter.exe")
  else:                  ("linux",   "nadeo-freeporter")

proc newGhClient(): HttpClient =
  result = newHttpClient(timeout = 120_000)
  result.headers = newHttpHeaders({
    "User-Agent": "forzamania/0.1",
    "Accept": "application/vnd.github+json",
  })

proc fetchLatestFreeporterAsset(keyword: string): (string, string) =
  ## Query GitHub for the latest release; return (download_url, tag) for the
  ## .zip asset whose name contains `keyword`. Raises if absent.
  let client = newGhClient()
  defer: client.close()
  let data = parseJson(client.getContent(FREEPORTER_LATEST_API))
  let tag = data{"tag_name"}.getStr("?")
  if data.hasKey("assets"):
    for asset in data["assets"]:
      let name = asset{"name"}.getStr("").toLowerAscii
      if keyword in name and name.endsWith(".zip"):
        return (asset["browser_download_url"].getStr, tag)
  raise newException(IOError,
    "no '" & keyword & "' .zip asset in " & FREEPORTER_REPO & " release " & tag)

proc downloadTo(url, dst: string) =
  ## Download `url` to `dst`, then magic-byte check it's a PKZIP (catches a 404/
  ## error page saved as a bogus "zip").
  createDir(parentDir(dst))
  let client = newGhClient()
  defer: client.close()
  client.downloadFile(url, dst)   # follows GitHub's redirect to the asset host
  var head = newString(2)
  let f = open(dst, fmRead)
  defer: f.close()
  let n = f.readBuffer(addr head[0], 2)
  if n < 2 or head != "PK":
    raise newException(IOError,
      "downloaded file from " & url & " is not a zip (first bytes: " & head &
      ") — the server probably returned an error page")

proc extractZip(zipPath, dstDir: string) =
  ## Extract a standard zip via the system `unzip` (preferred) or `tar`.
  createDir(dstDir)
  var rc = -1
  if findExe("unzip").len > 0:
    rc = execCmd("unzip -o -q " & quoteShell(zipPath) & " -d " & quoteShell(dstDir))
  if rc != 0 and findExe("tar").len > 0:
    rc = execCmd("tar -xf " & quoteShell(zipPath) & " -C " & quoteShell(dstDir))
  if rc != 0:
    raise newException(IOError,
      "failed to extract " & zipPath & " (need `unzip` or `tar` on PATH)")

proc downloadFreeporter*(toolsDir: string): DownloadResult =
  ## Download the latest nadeo-freeporter for this platform into `toolsDir`,
  ## extract it, and restore the ELF's exec bit (zip extraction drops it).
  let (keyword, binaryName) = freeporterAssetForPlatform()
  let (url, tag) = fetchLatestFreeporterAsset(keyword)

  createDir(toolsDir)
  let zipPath = toolsDir / ("nadeo-freeporter-" & keyword & "-" & tag & ".zip")
  downloadTo(url, zipPath)
  extractZip(zipPath, toolsDir)
  try: removeFile(zipPath)
  except OSError: discard

  var binary = toolsDir / binaryName
  if not fileExists(binary): binary = ""
  when not defined(windows):
    if binary.len > 0:
      # zip extraction can drop the exec bit; the runner must be able to spawn it.
      var perms = getFilePermissions(binary)
      perms.incl({fpUserExec, fpGroupExec, fpOthersExec})
      setFilePermissions(binary, perms)

  DownloadResult(name: "nadeo-freeporter", url: url, dstDir: toolsDir, binary: binary)

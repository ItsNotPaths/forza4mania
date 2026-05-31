## One-shot subprocess capture for the orchestrator.
##
## Nim port of the `run_captured` half of `src/subproc.py`. The Python version's
## temp-file dance existed only to dodge Wine's CreateProcess pipe-handle bug
## (WinError 6) when the app ran as a Windows .exe under Proton — the native Nim
## build never runs under Wine, so we use ordinary pipes. stderr is merged into
## stdout (poStdErrToStdOut) so a single readAll can't deadlock on a full pipe,
## and callers that want the error text still get it. The long-running lzxd
## daemon keeps its own bidirectional pipes (see lzx.nim).

import std/[osproc, streams]

type CapturedResult* = object
  rc*:     int
  output*: string   ## merged stdout + stderr

proc runCaptured*(command: string; args: seq[string]; workingDir = ""): CapturedResult =
  ## Run `command args` (optionally in `workingDir`), wait, and capture the
  ## merged stdout+stderr. Does not raise on a non-zero exit — the caller
  ## inspects `rc` (mirrors subprocess.CompletedProcess).
  let p = startProcess(command, workingDir = workingDir, args = args,
                       options = {poStdErrToStdOut})
  defer: p.close()
  result.output = p.outputStream.readAll()  # blocks until the child closes stdout
  result.rc = p.waitForExit()

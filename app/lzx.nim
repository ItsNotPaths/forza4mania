## lzxd_helper discovery, Turn 10 LZX chunk-framing, and the decode daemon.
##
## Nim port of `src/lzx.py`. The helper is a small C program linking libmspack's
## internal lzxd_* API; it decodes the Xbox-360 LZX flavour FM4's bin.zip uses.
## Decompression goes through one long-running daemon process (one fork/exec per
## extraction batch, then a binary stdin/stdout protocol per entry) — the same
## amortisation the Python side does over tens of thousands of entries.
##
## Daemon wire protocol (see src/lzxd_helper.c, big-endian u32s):
##   request : u32 out_len, u32 in_len, in_len bytes of stripped LZX stream
##   response: u32 status (0=ok), u32 payload_len, payload_len bytes
##             (decompressed bytes on status 0, else an ASCII error message)

import std/[os, osproc, streams, strutils]

const
  LZX_CHUNK_USIZE = 32768
  LZX_TRAILER     = 5

type
  DecompressionError* = object of CatchableError
  LzxDaemon* = object
    process*: Process
    inp: Stream
    outp: Stream

when defined(windows):
  const helperNames = ["lzxd_helper.exe", "lzxd_helper"]
else:
  const helperNames = ["lzxd_helper"]

proc findHelper*(): string =
  ## Locate the lzxd_helper binary. Search order mirrors src/lzx.py:
  ##   1. $FORZAMANIA_LZXD_HELPER override
  ##   2. <appdir>/tools/lzxd_helper   (release bundle layout)
  ##   3. <appdir>/lzxd_helper         (beside the binary)
  let override = getEnv("FORZAMANIA_LZXD_HELPER")
  if override.len > 0:
    if not fileExists(override):
      raise newException(IOError,
        "FORZAMANIA_LZXD_HELPER points at " & override & " which does not exist")
    return override
  let appDir = getAppDir()
  for d in [appDir / "tools", appDir]:
    for name in helperNames:
      let p = d / name
      if fileExists(p): return p
  raise newException(IOError,
    "lzxd_helper not found in " & (appDir / "tools") & " or " & appDir &
    ". Build it (release.sh --local) or set FORZAMANIA_LZXD_HELPER.")

# ---- chunk framing -------------------------------------------------------

proc stripChunkHeaders*(blob: string; uncompSize: int): string =
  ## Parse Turn 10's multi-chunk LZX framing into a continuous bitstream.
  ## Single-chunk entries: FF [u16 BE uncomp] [u16 BE comp] <stream> [5-byte trailer].
  ## Non-last chunks are prefixed with u16 BE csize; the last uses the FF form.
  var
    res = newStringOfCap(uncompSize)
    pos = 0
    remaining = uncompSize
  template b(i: int): int = blob[i].byte.int
  while remaining > 0:
    let last = remaining <= LZX_CHUNK_USIZE
    var step: int
    if last:
      if pos + 5 > blob.len:
        raise newException(DecompressionError, "truncated final chunk header at pos=" & $pos)
      if b(pos) != 0xFF:
        raise newException(DecompressionError,
          "expected 0xFF at final-chunk pos=" & $pos & ", got 0x" & toHex(b(pos), 2))
      let u = (b(pos+1) shl 8) or b(pos+2)
      let c = (b(pos+3) shl 8) or b(pos+4)
      pos += 5
      let endp = pos + c
      if endp + LZX_TRAILER > blob.len:
        raise newException(DecompressionError, "truncated final chunk body: comp=" & $c)
      res.add(blob[pos ..< endp])
      pos = endp + LZX_TRAILER
      step = u
    else:
      if pos + 2 > blob.len:
        raise newException(DecompressionError, "truncated chunk header at pos=" & $pos)
      let c = (b(pos) shl 8) or b(pos+1)
      pos += 2
      let endp = pos + c
      if endp > blob.len:
        raise newException(DecompressionError, "truncated chunk body: csize=" & $c)
      res.add(blob[pos ..< endp])
      pos = endp
      step = LZX_CHUNK_USIZE
    remaining -= step
  if pos != blob.len:
    raise newException(DecompressionError,
      "framing drift: ended at pos=" & $pos & ", blob len=" & $blob.len)
  return res

# ---- daemon client -------------------------------------------------------

proc writeU32BE(s: Stream; v: uint32) =
  s.write(char((v shr 24) and 0xFF))
  s.write(char((v shr 16) and 0xFF))
  s.write(char((v shr 8) and 0xFF))
  s.write(char(v and 0xFF))

proc readExact(s: Stream; n: int): string =
  ## Read exactly n bytes from a pipe stream; raise on short read (EOF).
  result = newString(n)
  var got = 0
  while got < n:
    let r = s.readData(addr result[got], n - got)
    if r <= 0:
      raise newException(DecompressionError,
        "lzxd_helper daemon closed pipe after " & $got & "/" & $n & " bytes")
    got += r

proc readU32BE(s: Stream): uint32 =
  let h = readExact(s, 4)
  (uint32(h[0].byte) shl 24) or (uint32(h[1].byte) shl 16) or
    (uint32(h[2].byte) shl 8) or uint32(h[3].byte)

proc startDaemon*(): LzxDaemon =
  let helper = findHelper()
  let p = startProcess(helper, args = ["daemon"], options = {})
  result = LzxDaemon(process: p, inp: p.inputStream, outp: p.outputStream)

proc close*(d: var LzxDaemon) =
  if d.process != nil:
    try:
      if d.inp != nil: d.inp.close()   # EOF on stdin → daemon exits its loop
      discard d.process.waitForExit()
    except CatchableError: discard
    finally:
      d.process.close()
      d.process = nil

proc decodeLzx*(d: var LzxDaemon; stream: string; outLen: int): string =
  ## Decompress one stripped LZX bitstream via the helper daemon.
  d.inp.writeU32BE(uint32(outLen))
  d.inp.writeU32BE(uint32(stream.len))
  if stream.len > 0:
    d.inp.writeData(unsafeAddr stream[0], stream.len)
  d.inp.flush()

  let status = readU32BE(d.outp)
  let payloadLen = int(readU32BE(d.outp))
  let payload = readExact(d.outp, payloadLen)
  if status != 0:
    raise newException(DecompressionError, "lzxd_helper: " & payload.strip())
  if payload.len != outLen:
    raise newException(DecompressionError,
      "lzxd output length " & $payload.len & " != expected " & $outLen)
  return payload

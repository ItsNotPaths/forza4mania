## Reader for Forza method-21 bin.zip containers (FM4 / FH1 shared format).
##
## Nim port of `src/binzip.py`. The file is a pseudo-zip: the central directory
## is standard PKZIP, but a method-21 entry's data may live directly at
## `headerOffset` with no local file header. Compression methods:
##   0   stored — raw bytes.
##   21  LZX (Xbox-360 flavour) with Turn 10's per-chunk framing.
##
## We parse the central directory by hand (Nim's zip wrappers can't decode
## method 21 and don't expose the raw header offset / compressed bytes we need).
## ZIP64 size/offset overflow fields are handled so multi-GB archives (FH1
## Colorado) still parse, though the FM4 tracks this targets stay under 4 GB.

import std/os
import lzx

type
  Entry* = object
    filename*:         string
    compressMethod*:   int
    headerOffset*:     int
    compressedSize*:   int
    uncompressedSize*: int

# ---- little-endian field readers (host is LE on every target) ------------

proc u16(s: string; p: int): int = s[p].byte.int or (s[p+1].byte.int shl 8)
proc u32(s: string; p: int): int64 =
  int64(s[p].byte) or (int64(s[p+1].byte) shl 8) or
    (int64(s[p+2].byte) shl 16) or (int64(s[p+3].byte) shl 24)
proc u64(s: string; p: int): int64 =
  var r: int64 = 0
  for i in 0 ..< 8: r = r or (int64(s[p+i].byte) shl (8*i))
  r

proc readAt(f: File; offset, n: int): string =
  result = newString(n)
  f.setFilePos(offset)
  if n > 0:
    let got = f.readBuffer(addr result[0], n)
    if got != n:
      raise newException(IOError, "short read: wanted " & $n & " got " & $got)

const U32MAX = 0xFFFFFFFF'i64

proc listEntries*(zipPath: string): seq[Entry] =
  ## Parse the central directory into Entry records (mirrors zipfile.infolist).
  let f = open(zipPath, fmRead)
  defer: f.close()
  let size = int(f.getFileSize())

  # Locate the End Of Central Directory record (PK\x05\x06) by scanning the
  # tail (the trailing comment can be up to 65535 bytes).
  let tailLen = min(size, 65557)
  let tail = readAt(f, size - tailLen, tailLen)
  var eocd = -1
  for i in countdown(tail.len - 22, 0):
    if tail[i] == 'P' and tail[i+1] == 'K' and tail[i+2] == '\x05' and tail[i+3] == '\x06':
      eocd = i; break
  if eocd < 0:
    raise newException(IOError, "no End Of Central Directory record in " & zipPath)

  var
    total   = u16(tail, eocd + 10)
    cdSize  = u32(tail, eocd + 12)
    cdOffset = u32(tail, eocd + 16)

  # ZIP64: if any field is saturated, the real values live in the ZIP64 EOCD,
  # found via the ZIP64 EOCD locator (PK\x06\x07) just before the EOCD.
  if cdOffset == U32MAX or cdSize == U32MAX or total == 0xFFFF:
    let loc = eocd - 20
    if loc >= 0 and tail[loc] == 'P' and tail[loc+1] == 'K' and
       tail[loc+2] == '\x06' and tail[loc+3] == '\x07':
      let z64 = u64(tail, loc + 8)
      let rec = readAt(f, int(z64), 56)
      total    = int(u64(rec, 32))
      cdSize   = u64(rec, 40)
      cdOffset = u64(rec, 48)

  let cd = readAt(f, int(cdOffset), int(cdSize))
  var p = 0
  for _ in 0 ..< total:
    if not (cd[p] == 'P' and cd[p+1] == 'K' and cd[p+2] == '\x01' and cd[p+3] == '\x02'):
      raise newException(IOError, "bad central-directory signature at " & $p)
    var e: Entry
    e.compressMethod   = u16(cd, p + 10)
    e.compressedSize   = int(u32(cd, p + 20))
    e.uncompressedSize = int(u32(cd, p + 24))
    let fnLen = u16(cd, p + 28)
    let exLen = u16(cd, p + 30)
    let cmLen = u16(cd, p + 32)
    e.headerOffset = int(u32(cd, p + 42))
    e.filename = cd[p + 46 ..< p + 46 + fnLen]

    # ZIP64 extra (id 0x0001): only the saturated fields are present, in the
    # fixed order usize, csize, lfh-offset, disk.
    if e.uncompressedSize == int(U32MAX) or e.compressedSize == int(U32MAX) or
       e.headerOffset == int(U32MAX):
      var ep = p + 46 + fnLen
      let exEnd = ep + exLen
      while ep + 4 <= exEnd:
        let id = u16(cd, ep)
        let dlen = u16(cd, ep + 2)
        var dp = ep + 4
        if id == 0x0001:
          if e.uncompressedSize == int(U32MAX): e.uncompressedSize = int(u64(cd, dp)); dp += 8
          if e.compressedSize == int(U32MAX):   e.compressedSize = int(u64(cd, dp)); dp += 8
          if e.headerOffset == int(U32MAX):     e.headerOffset = int(u64(cd, dp)); dp += 8
          break
        ep += 4 + dlen

    result.add(e)
    p += 46 + fnLen + exLen + cmLen

proc readEntry*(zipPath: string; entry: Entry; daemon: var LzxDaemon): string =
  ## Read (and decompress if needed) one entry's bytes.
  let f = open(zipPath, fmRead)
  defer: f.close()
  f.setFilePos(entry.headerOffset)
  var head = newString(4)
  discard f.readBuffer(addr head[0], 4)
  var blob: string
  if head == "PK\x03\x04":
    # A real local file header is present: skip past it to the data.
    let rest = readAt(f, entry.headerOffset + 4, 26)
    let fnLen = u16(rest, 22)
    let exLen = u16(rest, 24)
    blob = readAt(f, entry.headerOffset + 30 + fnLen + exLen, entry.compressedSize)
  else:
    # No local header: the 4 bytes we read are already the start of the data.
    blob = head & readAt(f, entry.headerOffset + 4, entry.compressedSize - 4)

  case entry.compressMethod
  of 0:
    return blob
  of 21:
    let stream = stripChunkHeaders(blob, entry.uncompressedSize)
    return decodeLzx(daemon, stream, entry.uncompressedSize)
  else:
    raise newException(DecompressionError,
      "unsupported compression method " & $entry.compressMethod & " for " & entry.filename)

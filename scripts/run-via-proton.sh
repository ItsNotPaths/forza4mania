#!/usr/bin/env bash
# Launch the released forzamania.exe via Proton, tee'ing all output to
# /tmp/forzamania-crash.log so we have something to read after Steam swallows
# the console.
#
# Adjust EXE / PROTON / COMPATDATA below if your paths differ.
set -u

EXE="/run/media/paths/SSS-Core/python projects/forzamania-release/forzamania.exe"
# Pick whichever Proton has the newest Wine. Experimental tracks mainline
# closest, so it's the most likely to implement obscure ucrtbase symbols
# (crealf, csqrt, etc.) that newer numpy builds rely on. GE-Proton10-34 is
# game-tuned and missed crealf as of 2026-05.
PROTON="/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Proton - Experimental/proton"
# PROTON="/home/paths/.steam/debian-installation/compatibilitytools.d/GE-Proton10-34/proton"
# PROTON="/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Proton 9.0 (Beta)/proton"
# PROTON="/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Proton Hotfix/proton"
# PROTON="/home/paths/.steam/debian-installation/steamapps/common/Proton 10.0/proton"
# A dedicated prefix per Proton so swapping between them doesn't trip the
# wineserver-version-mismatch check. Lives on a stable filesystem (NOT /tmp)
# so it survives reboots; the dirname suffix is the basename of $PROTON so
# each Proton gets its own.
PROTON_TAG="$(basename "$(dirname "$PROTON")" | tr ' ()' '_' )"
COMPATDATA="$HOME/.local/share/forzamania/prefix-${PROTON_TAG}"
LOG="/tmp/forzamania-crash.log"
mkdir -p "$COMPATDATA"

export STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.steam/steam"
export STEAM_COMPAT_DATA_PATH="$COMPATDATA"

[ -f "$EXE" ]    || { echo "missing exe: $EXE" >&2; exit 2; }
[ -x "$PROTON" ] || { echo "missing proton: $PROTON" >&2; exit 2; }

echo "[run] proton:     $PROTON"     | tee "$LOG"
echo "[run] exe:        $EXE"        | tee -a "$LOG"
echo "[run] compatdata: $COMPATDATA" | tee -a "$LOG"
echo "[run] log:        $LOG"
echo

"$PROTON" run "$EXE" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
echo
echo "[run] exit code: $RC" | tee -a "$LOG"
exit $RC

#!/usr/bin/env bash
# verify_texture_swap.sh — prove NadeoImporter rebakes the Custom* DDS into
# each .Item.Gbx, so per-chunk texture swapping is a viable strategy.
#
# Procedure:
#   1. Build a one-material FBX bundle (FBX + MeshParams.xml + Item.xml) using
#      Link="CustomConcrete" for every material slot.
#   2. Stage DDS file A as CustomConcrete_D.dds in the asset folder, run
#      NadeoImporter Mesh + Item, snapshot the resulting .Mesh.Gbx + .Item.Gbx.
#   3. Stage DDS file B in the same place, re-run, snapshot again.
#   4. Compare hashes:
#        - identical  → importer caches by Link name, plan is dead
#        - different  → importer rebakes per-invocation, plan is viable
#
# Why it runs against the *forzamania* prefix, not the TM2020 prefix:
#   When forzamania.exe normally runs under Proton (per run-via-proton.sh),
#   NadeoImporter resolves {userdocs} against the forzamania prefix's
#   Documents/, not TM2020's. See ui/convert_tab.py:374-379. So the asset
#   folder + Work/ files must live in the forzamania prefix to match what
#   the real pipeline does.

set -euo pipefail

# --- Config (mirrors scripts/run-via-proton.sh) -----------------------------
PROTON="/run/media/paths/SSS-Games/SteamLibrary/steamapps/common/Proton - Experimental/proton"
PROTON_TAG="$(basename "$(dirname "$PROTON")" | tr ' ()' '_' )"
COMPATDATA="$HOME/.local/share/forzamania/prefix-${PROTON_TAG}"
NADEO_EXE="/run/media/paths/SSS-Core/python projects/forzamania-release/tools/NadeoImporter.exe"

# Two visually distinct FM4 DDS files (picked from earlier Alps extraction).
# Pick a third one if you want to spot-check more variety in-game later.
DDS_A="/run/media/paths/SSS-Core/python projects/forzamania/working/full_blend/Alps_textures/_0x00000247.dds"
DDS_B="/run/media/paths/SSS-Core/python projects/forzamania/working/full_blend/Alps_textures/_0x000005A9.dds"

# A real Alps chunk FBX from an earlier run. Small (51 KB) but has actual
# geometry + 6 material slots, which we rebind to CustomConcrete below.
# xml_smoketest/TestChunk.fbx exists but is a 0-byte stub — don't use it.
SOURCE_FBX="/run/media/paths/SSS-Core/python projects/forzamania/working/fbx_center_test/Alps_Tile_n001_n006_00.fbx"

# --- Paths inside the forzamania prefix ------------------------------------
# NadeoImporter 2022_07_12 hardcodes {userdir} to <userdocs>\Trackmania2020,
# IGNORING Nadeo.ini's `UserDir={userdocs}\Trackmania` setting (verified via
# strace — it creates Trackmania2020/{Actions,Blocks,...,Work,Items} on its
# own and reads/writes only there). So our test files MUST live in
# Trackmania2020/Work, not Trackmania/Work.
USERDIR="${COMPATDATA}/pfx/drive_c/users/steamuser/Documents/Trackmania2020"
TEST_NAME="TexSwapSmoke"
WORK_REL_DIR="Items/Forzamania/${TEST_NAME}"
WORK_DIR="${USERDIR}/Work/${WORK_REL_DIR}"
# Outputs land in {userdir}/Items/Forzamania/<name>/ (NOT next to the input
# FBX in Work/). NadeoImporter splits inputs (Work/) from outputs (Items/).
OUT_DIR="${USERDIR}/${WORK_REL_DIR}"
ASSET_DIR="${USERDIR}/Items/_BlenderAssets/Textures/Stadium/CustomConcrete"
RESULTS_DIR="$(dirname "$(readlink -f "$0")")/../working/texture_swap_test"

# --- Sanity --------------------------------------------------------------
[ -x "$PROTON" ]    || { echo "missing Proton: $PROTON" >&2; exit 2; }
[ -f "$NADEO_EXE" ] || { echo "missing NadeoImporter: $NADEO_EXE" >&2; exit 2; }
[ -f "$SOURCE_FBX" ] || { echo "missing source FBX: $SOURCE_FBX" >&2; exit 2; }
[ -f "$DDS_A" ]     || { echo "missing DDS_A: $DDS_A" >&2; exit 2; }
[ -f "$DDS_B" ]     || { echo "missing DDS_B: $DDS_B" >&2; exit 2; }

mkdir -p "$WORK_DIR" "$ASSET_DIR" "$RESULTS_DIR"

# --- Build XML bundle -----------------------------------------------------
cp "$SOURCE_FBX" "$WORK_DIR/${TEST_NAME}.fbx"

# The Alps FBX above ships 6 material slots; FBX requires exact name match
# in MeshParams. We rebind every slot to Link="CustomConcrete" so the single
# staged DDS is what gets baked into the item.
cat > "$WORK_DIR/${TEST_NAME}.MeshParams.xml" <<EOF
<?xml version="1.0" ?>
<MeshParams Scale="1.0" MeshType="Static" Collection="Stadium" FbxFile="${TEST_NAME}.fbx">
    <Materials>
        <Material Name="Alps_Tile_n001_n006_00_000_road_blnd_trilin_2"     Link="CustomConcrete" />
        <Material Name="Alps_Tile_n001_n006_00_001_rdedg_blnd_diff_spec_bump_5" Link="CustomConcrete" />
        <Material Name="Alps_Tile_n001_n006_00_002_shldr_blnd_spec_2"      Link="CustomConcrete" />
        <Material Name="Alps_Tile_n001_n006_00_000_rdline_blnd_spec_opac_3" Link="CustomConcrete" />
        <Material Name="Alps_Tile_n001_n006_00_000_diff_opac_vlit_1"       Link="CustomConcrete" />
        <Material Name="Alps_Tile_n001_n006_00_000_shldr_blnd_spec_3"      Link="CustomConcrete" />
    </Materials>
    <Lights/>
</MeshParams>
EOF

cat > "$WORK_DIR/${TEST_NAME}.Item.xml" <<EOF
<?xml version="1.0" ?>
<Item AuthorName="forzamania" Collection="Stadium" Type="StaticObject">
    <MeshParamsLink File="${TEST_NAME}.MeshParams.xml" />
    <Phy/>
    <Vis/>
    <GridSnap HStep="0" VStep="0" HOffset="0" VOffset="0" />
    <Levitation HStep="0" VStep="0" HOffset="0" VOffset="0" GhostMode="false" />
    <Options AutoRotation="false" ManualPivotSwitch="false" NotOnItem="false" OneAxisRotation="false" />
    <PivotSnap Distance="0" />
</Item>
EOF

# --- Invocation helpers ----------------------------------------------------
run_importer() {
    local kind="$1"
    local rel="$2"     # path relative to Work/, with leading slash
    # `runinprefix` invokes wine synchronously instead of `run`'s daemonizing
    # game-launcher setup — that mode forks and returns immediately, so the
    # importer's output never lands and our snapshot check fires too early.
    STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.steam/steam" \
    STEAM_COMPAT_DATA_PATH="$COMPATDATA" \
    "$PROTON" runinprefix "$NADEO_EXE" "$kind" "$rel" 2>&1 | sed 's/^/    /'
}

normalize_outputs() {
    # NadeoImporter writes outputs as <Stem>.<Kind>.gbx — kind keeps its
    # CamelCase (Mesh/Shape/Item), the extension is lowercase. Rename
    # to .Gbx so we always reference one consistent form.
    mkdir -p "$OUT_DIR"
    for kind in Mesh Shape Item; do
        local lc="$OUT_DIR/${TEST_NAME}.${kind}.gbx"
        local cc="$OUT_DIR/${TEST_NAME}.${kind}.Gbx"
        [ -f "$lc" ] && [ ! -f "$cc" ] && mv "$lc" "$cc"
    done
    true
}

clean_outputs() {
    rm -f "$OUT_DIR/${TEST_NAME}".{Mesh,Shape,Item}.{Gbx,gbx}
}

snapshot_pass() {
    local label="$1"
    local mesh="$OUT_DIR/${TEST_NAME}.Mesh.Gbx"
    local item="$OUT_DIR/${TEST_NAME}.Item.Gbx"
    [ -f "$mesh" ] || { echo "FAIL[$label]: no .Mesh.Gbx produced (looking in $OUT_DIR)"; return 1; }
    [ -f "$item" ] || { echo "FAIL[$label]: no .Item.Gbx produced (looking in $OUT_DIR)"; return 1; }
    cp "$mesh" "$RESULTS_DIR/${TEST_NAME}_${label}.Mesh.Gbx"
    cp "$item" "$RESULTS_DIR/${TEST_NAME}_${label}.Item.Gbx"
}

# --- Run pass A ------------------------------------------------------------
echo "== PASS A — staging $(basename "$DDS_A")"
clean_outputs
cp "$DDS_A" "$ASSET_DIR/CustomConcrete_D.dds"
echo "    asset: $ASSET_DIR/CustomConcrete_D.dds ($(stat -c %s "$ASSET_DIR/CustomConcrete_D.dds") bytes)"
run_importer Mesh "/${WORK_REL_DIR}/${TEST_NAME}.fbx"
normalize_outputs
run_importer Item "/${WORK_REL_DIR}/${TEST_NAME}.Item.xml"
normalize_outputs
snapshot_pass A || exit 3

# --- Run pass B ------------------------------------------------------------
echo
echo "== PASS B — staging $(basename "$DDS_B")"
clean_outputs
cp "$DDS_B" "$ASSET_DIR/CustomConcrete_D.dds"
echo "    asset: $ASSET_DIR/CustomConcrete_D.dds ($(stat -c %s "$ASSET_DIR/CustomConcrete_D.dds") bytes)"
run_importer Mesh "/${WORK_REL_DIR}/${TEST_NAME}.fbx"
normalize_outputs
run_importer Item "/${WORK_REL_DIR}/${TEST_NAME}.Item.xml"
normalize_outputs
snapshot_pass B || exit 3

# --- Compare ---------------------------------------------------------------
echo
echo "== RESULTS"
for ext in Mesh.Gbx Item.Gbx; do
    a="$RESULTS_DIR/${TEST_NAME}_A.${ext}"
    b="$RESULTS_DIR/${TEST_NAME}_B.${ext}"
    sa=$(stat -c %s "$a"); sb=$(stat -c %s "$b")
    ha=$(sha256sum "$a" | cut -d' ' -f1)
    hb=$(sha256sum "$b" | cut -d' ' -f1)
    same="DIFFER"
    [ "$ha" = "$hb" ] && same="IDENTICAL"
    echo "  $ext  A: ${sa} bytes  ${ha:0:16}…"
    echo "  $ext  B: ${sb} bytes  ${hb:0:16}…"
    echo "  $ext  → $same"
done

echo
echo "Snapshots saved to: $RESULTS_DIR"

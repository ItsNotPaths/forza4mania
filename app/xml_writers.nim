## Emit MeshParams.xml + Item.xml that freeporter (ex-NadeoImporter) accepts.
##
## Nim port of `src/xml_writers.py`. Byte-for-byte the same XML text the Python
## writer produces — freeporter's `mesh`/`item` subcommands read these sibling
## XMLs next to the FBX. STOCK Stadium Links only: NO BaseTexture (the importer
## silently aborts on a Stadium material that carries one). Collection is the
## short env name "Stadium" (the importer rejects "Stadium2020"). v1 items are
## bare StaticObjects — no light / waypoint / pivot.

import std/[os, strutils]
import materials

proc escape(s: string): string =
  ## Minimal XML attribute escaping. `&` MUST be replaced first so the later
  ## entity ampersands aren't double-escaped — same order as materials.py.
  s.replace("&", "&amp;").replace("\"", "&quot;")
   .replace("<", "&lt;").replace(">", "&gt;")

proc withSuffix(fbxPath, suffix: string): string =
  ## Replace the final extension, mirroring pathlib `Path.with_suffix`.
  let (dir, name, _) = splitFile(fbxPath)
  dir / (name & suffix)

proc writeMeshParams*(fbxPath: string; materials: openArray[TM2020Material];
                      scale = 1.0): string =
  ## Write <fbx>.MeshParams.xml next to the FBX; return the XML path.
  let xmlPath = withSuffix(fbxPath, ".MeshParams.xml")

  var rows: seq[string]
  for m in materials:
    var attrs = @["Name=\"" & escape(m.name) & "\"",
                  "Link=\"" & escape(m.link) & "\""]
    if m.physicsId.len > 0:
      attrs.add("PhysicsId=\"" & escape(m.physicsId) & "\"")
    rows.add("        <Material " & attrs.join(" ") & " />")
  let materialsBlock = if rows.len > 0: rows.join("\n") else: ""

  let xml =
    "<?xml version=\"1.0\" ?>\n" &
    "<MeshParams Scale=\"" & $scale & "\" MeshType=\"Static\" Collection=\"Stadium\" " &
    "FbxFile=\"" & escape(extractFilename(fbxPath)) & "\">\n" &
    "    <Materials>\n" &
    materialsBlock & "\n" &
    "    </Materials>\n" &
    "    <Lights/>\n" &
    "</MeshParams>\n"
  writeFile(xmlPath, xml)
  return xmlPath

proc writeItemXml*(fbxPath: string; author = "forzamania"): string =
  ## Write <fbx>.Item.xml next to the FBX; return the XML path. Bare
  ## StaticObject: no waypoint, no pivots, no levitation snap.
  let xmlPath = withSuffix(fbxPath, ".Item.xml")
  let (_, name, _) = splitFile(fbxPath)
  let meshparamsFilename = name & ".MeshParams.xml"

  let xml =
    "<?xml version=\"1.0\" ?>\n" &
    "<Item AuthorName=\"" & escape(author) & "\" Collection=\"Stadium\" Type=\"StaticObject\">\n" &
    "    <MeshParamsLink " &
    "File=\"" & escape(meshparamsFilename) & "\" />\n" &
    "    <Phy/>\n" &
    "    <Vis/>\n" &
    "    <GridSnap HStep=\"0\" VStep=\"0\" HOffset=\"0\" VOffset=\"0\" />\n" &
    "    <Levitation HStep=\"0\" VStep=\"0\" HOffset=\"0\" VOffset=\"0\" GhostMode=\"false\" />\n" &
    "    <Options AutoRotation=\"false\" ManualPivotSwitch=\"false\" NotOnItem=\"false\" OneAxisRotation=\"false\" />\n" &
    "    <PivotSnap Distance=\"0\" />\n" &
    "</Item>\n"
  writeFile(xmlPath, xml)
  return xmlPath

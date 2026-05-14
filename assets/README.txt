Drop seed assets needed at runtime here.

Currently expected:
  empty_stadium.Map.Gbx — a tiny "blank" TM2020 stadium map exported once
                          from TM2020's in-game editor. Used by the map
                          composer as the starting point — the dotnet helper
                          stamps items + base ground blocks into a copy of
                          this seed to produce the final .Map.Gbx.

If this file is missing the pipeline still produces .Item.Gbx files; only
the final automatic .Map.Gbx composition step is skipped.

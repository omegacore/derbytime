# Rank emblems for the standings board

Web-ready Scouting America rank emblems, one per rank, named by theme:

    lion  tiger  wolf  bear  webelos  aol  pack

`.svg`, `.webp` or `.png` (first match wins, case-insensitive). Each is shown
at its own shape in the slide header; transparent backgrounds look best.

These PNGs are generated from the full-size masters in `assets/rank-emblems/`
(white surround made transparent, trimmed, 512px on the long side). To rebuild
after replacing a master, run `assets/rank-emblems/build.py` with Pillow:

    python assets/rank-emblems/build.py .

Any rank without a file here uses the built-in drawn icon. `pack.*` is used
for finals and the Pack Overall slides (e.g. your pack's own logo).

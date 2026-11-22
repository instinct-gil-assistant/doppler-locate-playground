"""Minimal, disclosed patch: let the candidate grid CRS be set for non-CONUS regions."""
p = "/opt/doppler-locate/src/dopploc/geo.py"
s = open(p).read()
s = s.replace("""from functools import lru_cache
from pathlib import Path""", """import os
from functools import lru_cache
from pathlib import Path""")
s = s.replace('GRID_CRS = "EPSG:5070"  # NAD83 / Conus Albers, metres; candidate grids live on this lattice',
              'GRID_CRS = os.environ.get("DOPPLOC_GRID_CRS", "EPSG:5070")  # grid CRS (default CONUS Albers); DOPPLOC_GRID_CRS sets a local grid for other regions')
assert "DOPPLOC_GRID_CRS" in s, "patch anchor not found"
open(p, "w").write(s)
print("patched", p)

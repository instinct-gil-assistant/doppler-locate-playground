"""Score-step runner: runs in its own process so the Gradio web process never imports
the engine (512 MB free-tier budget). Mirrors `dopploc score` but with the playground's
parameters: blind trim to DOPPLOC_MAX_SCORE by |offset_err| before scoring,
refine=False, fine_top=300. Prints a machine-readable CANDIDATES line for the web app.

Usage: python score_runner.py CASE_TOML [REPORT_TOP]
Exit 3 when no candidate survived the silence filter.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("DOPPLOC_SRC", "/opt/doppler-locate/src"))

import polars as pl  # noqa: E402

from dopploc import config, report, score  # noqa: E402


def main() -> int:
    case = config.load(Path(sys.argv[1]))
    top = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    max_score = int(os.environ.get("DOPPLOC_MAX_SCORE", "2500"))

    cand = pl.read_parquet(case.out / "candidates.parquet")
    if "heard_between" in cand.columns:
        cand = cand.filter(pl.col("heard_between") == 0)
    n_all = len(cand)
    if n_all == 0:
        print("CANDIDATES all=0 scored=0 trimmed=False")
        return 3
    trimmed = n_all > max_score
    if trimmed:
        cand = cand.sort(pl.col("offset_err").abs()).head(max_score)
    print(f"CANDIDATES all={n_all} scored={len(cand)} trimmed={trimmed}", flush=True)

    res = score.score_candidates(case, cand, refine=False, fine_top=300)
    report.write(case, res, top=top)
    return 0


if __name__ == "__main__":
    sys.exit(main())

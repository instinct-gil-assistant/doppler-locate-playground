"""Doppler Locate - web playground for github.com/digitalis-p/doppler-locate (MIT).

Upload a recording, find propeller-aircraft Doppler curves, then locate where the
recording was made by matching them against public ADS-B flight history.

Free-tier design: one job at a time, regional search on a local azimuthal-equidistant
grid (DOPPLOC_GRID_CRS), bounded scoring, uploads deleted right after each run.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import gradio as gr

ROOT = Path(os.environ.get("DOPPLOC_WORK", "/tmp/doppler-web-work"))
JOBS = ROOT / "jobs"
DATA = Path(os.environ.get("DOPPLOC_DATA", str(ROOT / "data")))
JOBS.mkdir(parents=True, exist_ok=True)
DATA.mkdir(parents=True, exist_ok=True)

APP_KEY = os.environ.get("DOPPLOC_APP_KEY", "")
MAX_AUDIO_S = 20 * 60
MAX_RADIUS_KM = 60
MAX_SCORE_CANDIDATES = int(os.environ.get("DOPPLOC_MAX_SCORE", "2500"))
OFFSET_TOL_S = 10.0
JOB_LOCK = threading.Lock()
GH_REPO_API = "https://api.github.com/repos/adsblol/globe_history_{year}/releases/tags/v{tag}-planes-readsb-prod-0"

sys.path.insert(0, os.environ.get("DOPPLOC_SRC", "/opt/doppler-locate/src"))
from dopploc import adsb, config, detect, geo, report, score  # noqa: E402

BANNER = f"""## Doppler Locate - playground

Find where a recording was made from the Doppler curves of propeller aircraft, matched
against public ADS-B flight history. Web front-end for the open-source engine
[digitalis-p/doppler-locate](https://github.com/digitalis-p/doppler-locate) (MIT),
an independent re-implementation of the method from colsto's video.

**How to play**
1. Upload a recording (a phone voice memo works) that caught one or two planes passing.
2. **Detect** finds the aircraft Doppler curves in it.
3. Enter the recording's **UTC date** and the **rough area** (up to {MAX_RADIUS_KM} km radius),
   then **Locate** searches that region for the spot whose audible sky matches.

**What to expect on free community compute**
- First search on a new day downloads ~4 GB of public flight history (one-time per day
  and region, a few minutes) and builds a regional track table (a few more minutes).
- Each search then takes from under a minute to several minutes, depending on traffic in
  the region. Progress is shown live; keep the tab open.
- ADS-B history is available for recent months (adsb.lol). Recordings need propeller
  aircraft passing within a few kilometres; jets and quiet electric drones won't register.
- Scoring here models still air (wind aloft is not fetched in this build), so expect
  the right neighbourhood rather than survey-grade coordinates.

**Privacy:** uploads are analysed in this session, then deleted (always within ~30
minutes). Only anonymous public flight, terrain and weather data is cached. Nothing is
shared. One analysis runs at a time; if the app is busy, try again in a few minutes.
"""


# ----------------------------------------------------------------- helpers

def _check_key(key: str):
    if APP_KEY and key.strip() != APP_KEY:
        raise gr.Error("Wrong access key.")


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True)
    return float(out.stdout.strip())


def _sweeper():
    while True:
        time.sleep(600)
        try:
            for d in JOBS.iterdir():
                if time.time() - d.stat().st_mtime > 1800:
                    shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def _trim_data():
    """Keep the data cache under ~35 GB by deleting oldest ingested day/region dirs."""
    def size(p):
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    dirs = [p for p in DATA.iterdir() if p.is_dir() and p.name != "raw" and p.name != "ref"]
    total = size(DATA)
    while total > 35 << 30 and dirs:
        old = min(dirs, key=lambda d: d.stat().st_mtime)
        total -= size(old)
        shutil.rmtree(old, ignore_errors=True)
        dirs.remove(old)


threading.Thread(target=_sweeper, daemon=True).start()


def fetch_day_public(day: str, out_dir: Path, log) -> list[Path]:
    """Download one UTC day's split tar from the public adsb.lol release (no gh CLI)."""
    import requests
    y, m, d = day.split("-")
    tag = f"v{y}.{m}.{d}-planes-readsb-prod-0"
    out_dir.mkdir(parents=True, exist_ok=True)
    have = sorted(out_dir.glob(f"{tag}.tar.*"))
    if have:
        return have
    r = requests.get(GH_REPO_API.format(year=y, tag=f"{y}.{m}.{d}"), timeout=60)
    if r.status_code != 200:
        raise gr.Error(f"No ADS-B history release for {day} (HTTP {r.status_code}). "
                       "Only days covered by adsb.lol globe history work.")
    assets = [a for a in r.json().get("assets", []) if ".tar." in a["name"]]
    if not assets:
        raise gr.Error(f"Release for {day} has no tar assets.")
    parts = []
    for a in assets:
        dest = out_dir / a["name"]
        log(f"downloading {a['name']} ({a['size'] / 1e9:.1f} GB) ...")
        url = a["browser_download_url"]
        with requests.get(url, stream=True, timeout=600) as resp:
            resp.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            n = 0
            t0 = time.time()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(1 << 22):
                    f.write(chunk)
                    n += len(chunk)
                    if time.time() - t0 > 20:
                        t0 = time.time()
                        log(f"  {n / 1e9:.1f} GB ...")
            tmp.rename(dest)
        parts.append(dest)
    return sorted(parts)


def ensure_day(day: str, bbox, log) -> Path:
    key = f"{day}_{bbox[0]:.2f}_{bbox[1]:.2f}_{bbox[2]:.2f}_{bbox[3]:.2f}"
    out = DATA / key
    if (out / "aircraft.parquet").exists():
        log(f"flight history for {day} over this region: cached")
        return out
    raw = DATA / "raw" / day
    parts = sorted(raw.glob("*.tar.*"))
    if not parts:
        parts = fetch_day_public(day, raw, log)
    types = DATA / "ref" / "icao_aircraft_types2.js.gz"
    if not types.exists():
        adsb.fetch_types(types)
    log("building the regional track table (a few minutes) ...")
    res = adsb.ingest(parts, out, types, bbox=bbox, workers=2, rows_per_file=2_000_000)
    log(f"track table: {res['aircraft_in_bbox']:,} aircraft, {res['points']:,} points in region")
    _trim_data()
    return out


# ----------------------------------------------------------------- step 1: detect

def do_detect(key, file_path, url, start, dur):
    _check_key(key)
    if not JOB_LOCK.acquire(blocking=False):
        raise gr.Error("Another analysis is running - try again in a few minutes.")
    job = JOBS / uuid.uuid4().hex[:12]
    job.mkdir(parents=True)
    lines = []

    def log(*a):
        lines.append(" ".join(str(x) for x in a))

    try:
        yield "\n".join(lines), None, None, None, None, None, gr.update(choices=[], value=[]), ""
        if file_path:
            src = job / ("upload" + Path(file_path).suffix)
            shutil.copy(file_path, src)
        elif url and url.strip():
            log("downloading audio track ...")
            yield "\n".join(lines), None, None, None, None, None, gr.update(), ""
            try:
                src, meta = detect.fetch(url.strip(), job / "dl", log=log)
            except Exception as e:
                raise gr.Error(f"Could not download that URL ({type(e).__name__}). "
                               "YouTube and some sites block datacenter IPs; upload the file instead.")
        else:
            raise gr.Error("Upload a recording or paste a direct media URL.")
        total = _ffprobe_duration(src)
        start = max(0.0, float(start or 0.0))
        dur = float(dur or 0.0)
        if start >= total:
            raise gr.Error(f"Start ({start:.0f} s) is beyond the recording length ({total:.0f} s).")
        if dur <= 0 or start + dur > total:
            dur = total - start
        if dur > MAX_AUDIO_S:
            dur = float(MAX_AUDIO_S)
            log(f"analysing only {MAX_AUDIO_S // 60} min of the selected range (free-tier cap)")
        log(f"recording {total / 60:.1f} min; analysing {start:.0f}-{start + dur:.0f} s ...")
        yield "\n".join(lines), None, None, None, None, None, gr.update(), ""
        t0 = time.time()
        outdir = job / "detect"
        proc = subprocess.Popen([sys.executable, "-m", "dopploc.cli", "detect", str(src),
                                 "--start", str(start), "--duration", str(dur),
                                 "--out", str(outdir)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env=dict(os.environ))
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line and not line.startswith("|") and not line.startswith("source:"):
                log(line)
        proc.wait(timeout=3600)
        if proc.returncode != 0:
            raise gr.Error("Detection failed; see log.")
        log(f"detection took {time.time() - t0:.0f} s")

        rep = (outdir / "report.md").read_text()
        det = json.loads((outdir / "detections.json").read_text())
        import tomllib
        events_doc = tomllib.loads((outdir / "events.toml").read_text()) if (outdir / "events.toml").exists() else {}
        events = events_doc.get("event", [])
        fly = det.get("flyovers", [])
        strong = [f for f in fly if f.get("confidence") != "low" and f.get("kind") == "aircraft"]
        choices = [f"{n}: t={detect.fmt_time(f['t_cpa'])} f0={f['f0']:.0f} Hz v={f['v']:.0f} m/s "
                   f"d={f['d']:.0f} m ({f['confidence']})" for n, f in enumerate(strong)]
        default = choices[:2] if len(choices) >= 2 else choices
        state = {"job": str(job), "src": str(src), "events": events, "strong": strong}
        png = str(outdir / "detections.png") if (outdir / "detections.png").exists() else None
        yield ("\n".join(lines) + "\n\n" + rep, rep, png, state, str(outdir / "events.toml"),
               str(outdir / "detections.json"), gr.update(choices=choices, value=default), "")
    finally:
        JOB_LOCK.release()


# ----------------------------------------------------------------- step 2: locate

def do_locate(key, state, picks, day, lat, lon, radius_km):
    _check_key(key)
    if not state or not state.get("events"):
        raise gr.Error("Run Detect first.")
    picks = picks or []
    if len(picks) != 2:
        raise gr.Error("Pick exactly two detected flyovers (the search matches pairs).")
    idx = [int(p.split(":")[0]) for p in picks]
    ev = []
    for n, i in enumerate(idx):
        if i >= len(state["events"]):
            raise gr.Error("Pick mismatch - re-run Detect.")
        e = dict(state["events"][i])
        e["name"] = f"D{n + 1}"
        ev.append(e)
    ev.sort(key=lambda e: e["audio_cpa"])
    ev[0]["name"], ev[1]["name"] = "D1", "D2"

    try:
        datetime.fromisoformat(day)
    except Exception:
        raise gr.Error("UTC date must look like 2026-09-08.")
    lat, lon = float(lat), float(lon)
    if not (-89 < lat < 89 and -180 < lon < 180):
        raise gr.Error("Centre latitude/longitude out of range.")
    radius_km = min(float(radius_km), MAX_RADIUS_KM)
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.2, abs(math.cos(math.radians(lat)))))
    bbox = (lon - dlon, lat - dlat, lon + dlon, lat + dlat)

    if not JOB_LOCK.acquire(blocking=False):
        raise gr.Error("Another analysis is running - try again in a few minutes.")
    job = Path(state["job"])
    out = job / "locate"
    out.mkdir(parents=True, exist_ok=True)
    lines = []

    def log(*a):
        lines.append(" ".join(str(x) for x in a))

    try:
        log(f"region: {radius_km:.0f} km around {lat:.4f},{lon:.4f}; day {day} (UTC)")
        yield "\n".join(lines), None, None, None, None
        crs = (f"+proj=aeqd +lat_0={lat:.5f} +lon_0={lon:.5f} +datum=WGS84 +units=m +no_defs")
        os.environ["DOPPLOC_GRID_CRS"] = crs
        geo.GRID_CRS = crs
        geo._transformers.cache_clear()

        adsb_dir = ensure_day(day, bbox, log)
        yield "\n".join(lines), None, None, None, None

        case_toml = out / "case.toml"
        ev_blocks = "\n\n".join(
            "[[event]]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in e.items()) for e in ev)
        case_toml.write_text(f"""name = "web"
day = "{day}"
adsb = "{adsb_dir}"
cache = "{DATA / 'cache'}"
audio = "{state['src']}"
out = "{out}"
c = 343.0
wind = "none"

[search]
bbox = [{bbox[0]:.5f}, {bbox[1]:.5f}, {bbox[2]:.5f}, {bbox[3]:.5f}]
elevation_lattice = false
gpu = false
offset_tol_s = {OFFSET_TOL_S}
workers = 2

[silence]
enabled = true
mode = "cpa"
ratio = 1.0

{ev_blocks}
""")
        log("searching the region ...")
        yield "\n".join(lines), None, None, None, None
        env = dict(os.environ)
        proc = subprocess.run([sys.executable, "-m", "dopploc.cli", "search", str(case_toml)],
                              capture_output=True, text=True, env=env, timeout=1800)
        lines += [l for l in proc.stdout.strip().splitlines() if l.strip()][-12:]
        if proc.returncode != 0:
            log(proc.stderr[-1500:])
            raise gr.Error("Search failed; see log.")
        yield "\n".join(lines), None, None, None, None

        import polars as pl
        cand = pl.read_parquet(out / "candidates.parquet")
        cand = cand.filter(pl.col("heard_between") == 0)
        n_all = len(cand)
        if n_all == 0:
            raise gr.Error("No candidate spot in this region matched both flyovers. "
                           "Try a wider region, a different day, or other detections.")
        trimmed = False
        if n_all > MAX_SCORE_CANDIDATES:
            cand = cand.sort(pl.col("offset_err").abs()).head(MAX_SCORE_CANDIDATES)
            trimmed = True
        log(f"modelling and scoring {len(cand)} candidate spots"
            + (f" (of {n_all:,}; kept the {MAX_SCORE_CANDIDATES:,} with the tightest event timing)" if trimmed else "")
            + " ...")
        yield "\n".join(lines), None, None, None, None
        t0 = time.time()
        case = config.load(case_toml)
        res = score.score_candidates(case, cand, log=log, refine=False, fine_top=300)
        report.write(case, res, top=15)
        log(f"scoring took {time.time() - t0:.0f} s")

        best = pl.read_csv(out / "best_per_pair.csv")
        top = best.head(25)
        rows = []
        for i, r in enumerate(top.iter_rows(named=True), 1):
            rows.append({"rank": i, "lat": round(r["lat"], 5), "lon": round(r["lon"], 5),
                         "score": round(r["score"], 3),
                         "flight 1": r["f1"].split(":")[0], "flight 2": r["f2"].split(":")[0],
                         "map": f"https://www.openstreetmap.org/?mlat={r['lat']:.5f}&mlon={r['lon']:.5f}#map=16/{r['lat']:.5f}/{r['lon']:.5f}"})
        import folium
        m = folium.Map(location=[lat, lon], zoom_start=10)
        folium.Circle([lat, lon], radius=radius_km * 1000, color="#888", weight=1,
                      fill=False, dash_array="4").add_to(m)
        for i, r in enumerate(top.head(15).iter_rows(named=True), 1):
            color = "#d33" if i == 1 else ("#e83e8c" if i <= 3 else "#2b7bbb")
            folium.CircleMarker([r["lat"], r["lon"]], radius=7 if i == 1 else 5, color=color,
                                fill=True, fill_opacity=0.85,
                                popup=f"#{i} score {r['score']:.2f}").add_to(m)
        map_html = m._repr_html_()
        summary = (out / "summary.md").read_text() if (out / "summary.md").exists() else ""
        rank1 = str(out / "rank1.png") if (out / "rank1.png").exists() else None
        log("done.")
        yield ("\n".join(lines), summary, rank1, map_html, rows)
    finally:
        JOB_LOCK.release()


# ----------------------------------------------------------------- UI

with gr.Blocks(title="Doppler Locate playground") as demo:
    gr.Markdown(BANNER)
    key_in = gr.Textbox(label="Access key", type="password",
                        placeholder="the key from the message that shared this link")
    with gr.Group():
        gr.Markdown("### 1 - Find aircraft in a recording")
        with gr.Row():
            file_in = gr.File(label="Audio or video file", file_count="single")
            url_in = gr.Textbox(label="... or direct media URL",
                                placeholder="https://... (YouTube may be blocked)")
        with gr.Row():
            start_in = gr.Number(label="start (s)", value=0, minimum=0)
            dur_in = gr.Number(label="analyse at most (s; 0 = whole)", value=0, minimum=0)
            detect_btn = gr.Button("Detect flyovers", variant="primary")
    detect_log = gr.Textbox(label="progress", lines=8, interactive=False)
    detect_report = gr.Markdown()
    detect_img = gr.Image(label="spectrogram with detected flyovers", type="filepath")
    with gr.Row():
        events_dl = gr.File(label="events.toml")
        detections_dl = gr.File(label="detections.json")
    detect_state = gr.State({})

    with gr.Group():
        gr.Markdown("### 2 - Locate the recording")
        picks_in = gr.CheckboxGroup(label="two flyovers to match (from the detection above)", choices=[])
        with gr.Row():
            day_in = gr.Textbox(label="UTC date of the recording", placeholder="2026-11-21")
        with gr.Row():
            lat_in = gr.Number(label="centre latitude", value=32.08)
            lon_in = gr.Number(label="centre longitude", value=34.78)
            radius_in = gr.Slider(label="search radius (km)", minimum=10, maximum=MAX_RADIUS_KM,
                                  value=30, step=5)
        locate_btn = gr.Button("Locate", variant="primary")
    locate_log = gr.Textbox(label="progress", lines=10, interactive=False)
    locate_summary = gr.Markdown()
    locate_img = gr.Image(label="winning pair: modelled curves over the recording", type="filepath")
    locate_map = gr.HTML()
    locate_table = gr.Dataframe(label="top candidate spots", interactive=False)

    detect_btn.click(do_detect, [key_in, file_in, url_in, start_in, dur_in],
                     [detect_log, detect_report, detect_img, detect_state, events_dl, detections_dl,
                      picks_in, locate_log])
    locate_btn.click(do_locate, [key_in, detect_state, picks_in, day_in, lat_in, lon_in, radius_in],
                     [locate_log, locate_summary, locate_img, locate_map, locate_table])

    gr.Markdown(
        "---\nEngine: [digitalis-p/doppler-locate](https://github.com/digitalis-p/doppler-locate) (MIT). "
        "Data: [adsb.lol](https://adsb.lol) globe history (ODbL), Copernicus GLO-90 DEM, EGM96 geoid. "
        "Runs on free community compute; not affiliated with the engine's author. "
        "Use only on your own recordings or with the consent of whoever made them.")

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=4).launch(server_name="0.0.0.0", server_port=7860,
                                                   max_file_size="150mb",
                                                   allowed_paths=[str(JOBS)])

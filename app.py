"""Doppler Locate - web playground for github.com/digitalis-p/doppler-locate (MIT).

Upload a recording, find propeller-aircraft Doppler curves, then locate where the
recording was made by matching them against public ADS-B flight history.

Free-tier design: one job at a time, regional search on a local azimuthal-equidistant
grid (DOPPLOC_GRID_CRS), bounded scoring, uploads deleted right after each run.

Memory design (512 MB host): this web process imports NO engine modules. Every heavy
step (type table, tar ingest, search, score) runs as a subprocess, and each step's
peak child memory is logged.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import resource
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from datetime import datetime
from pathlib import Path

import hmac as _hmac

import gradio as gr
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

ROOT = Path(os.environ.get("DOPPLOC_WORK", "/tmp/doppler-web-work"))
JOBS = ROOT / "jobs"
DATA = Path(os.environ.get("DOPPLOC_DATA", str(ROOT / "data")))
JOBS.mkdir(parents=True, exist_ok=True)
DATA.mkdir(parents=True, exist_ok=True)

APP_KEY = os.environ.get("DOPPLOC_APP_KEY", "")
# Server-side gate: the key reaches the server once, in a POST body (never in a URL,
# so it is not logged or leaked via referrer); the server sets an HttpOnly cookie that
# every upload, job-action, queue and file route must then carry.
import hashlib
AUTH_COOKIE = "dopploc_auth"
AUTH_TOKEN = hashlib.sha256(("dopploc-gate:" + APP_KEY).encode()).hexdigest() if APP_KEY else ""
PROTECTED_PREFIXES = ("/upload", "/file", "/queue", "/gradio_api")

fast = FastAPI()


@fast.middleware("http")
async def _gate(request, call_next):
    if AUTH_TOKEN and request.url.path == "/" and request.cookies.get(AUTH_COOKIE) != AUTH_TOKEN:
        return HTMLResponse("<html><body style='font:18px sans-serif;max-width:36rem;margin:4rem auto'><h1>Doppler Locate</h1><p>Open your private invitation link to enter. No access key is needed.</p><p>If your invitation was already used on another browser, ask for a fresh invitation.</p></body></html>")
    if AUTH_TOKEN and request.url.path.startswith(PROTECTED_PREFIXES):
        if request.cookies.get(AUTH_COOKIE) != AUTH_TOKEN:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
    return await call_next(request)


@fast.post("/dopploc-auth")
async def _dopploc_auth(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if APP_KEY and str(data.get("key", "")).strip() == APP_KEY:
        r = JSONResponse({"ok": True})
        r.set_cookie(AUTH_COOKIE, AUTH_TOKEN, httponly=True, secure=True,
                     samesite="lax", max_age=60 * 60 * 24 * 30)
        return r
    return JSONResponse({"ok": False}, status_code=403)


USED_INVITES = DATA / "used_invites.json"
INVITE_LOCK = threading.Lock()


def _invite_ok(exp: int, sig: str) -> bool:
    if not (APP_KEY and exp and sig):
        return False
    good = _hmac.new(APP_KEY.encode(), f"invite:{exp}".encode(), hashlib.sha256).hexdigest()
    return _hmac.compare_digest(sig.strip(), good) and time.time() < exp


def _used_invites():
    try:
        return set(json.loads(USED_INVITES.read_text()))
    except FileNotFoundError:
        return set()


@fast.get("/dopploc-invite")
async def _dopploc_invite_page(exp: int = 0, sig: str = ""):
    if not _invite_ok(exp, sig) or not AUTH_TOKEN:
        return HTMLResponse("<p>This invitation is invalid or expired.</p>", status_code=403)
    if sig in _used_invites():
        return HTMLResponse("<p>This invitation was already used. Open the app in the browser where you first used it, or ask for a fresh invitation.</p>", status_code=410)
    return HTMLResponse(
        "<html><body style='font:18px sans-serif;max-width:36rem;margin:4rem auto'>"
        "<h1>Doppler Locate - playground</h1><p>Open the app on this browser. No access key is needed. You stay signed in for 30 days.</p>"
        f"<form method='post' action='/dopploc-invite'><input type='hidden' name='exp' value='{exp}'>"
        f"<input type='hidden' name='sig' value='{sig}'>"
        "<button style='font-size:1.1rem;padding:.8rem 1.4rem'>Open the playground</button></form></body></html>")


@fast.post("/dopploc-invite")
async def _dopploc_invite_redeem(request: Request):
    form = await request.form()
    try:
        exp = int(str(form.get("exp", "0")))
    except ValueError:
        return JSONResponse({"ok": False}, status_code=403)
    sig = str(form.get("sig", ""))
    with INVITE_LOCK:
        if not _invite_ok(exp, sig) or not AUTH_TOKEN:
            return JSONResponse({"ok": False}, status_code=403)
        used = _used_invites()
        if sig in used:
            return HTMLResponse("<p>This invitation was already used.</p>", status_code=410)
        used.add(sig)
        tmp = USED_INVITES.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(used)))
        tmp.replace(USED_INVITES)
    r = RedirectResponse("/", status_code=303)
    r.set_cookie(AUTH_COOKIE, AUTH_TOKEN, httponly=True, secure=True,
                 samesite="lax", max_age=60 * 60 * 24 * 30)
    return r


MAX_AUDIO_S = 20 * 60
MAX_RADIUS_KM = 60
MAX_SCORE_CANDIDATES = int(os.environ.get("DOPPLOC_MAX_SCORE", "2500"))
OFFSET_TOL_S = 10.0
JOB_LOCK = threading.Lock()
GH_REPO_API = "https://api.github.com/repos/adsblol/globe_history_{year}/releases/tags/v{tag}-planes-readsb-prod-0"
ENGINE_SRC = os.environ.get("DOPPLOC_SRC", "/opt/doppler-locate/src")
RUNNER = Path(__file__).parent / "score_runner.py"

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
- Scoring here models still air (wind aloft is not fetched in this build), and hosted validation did not recover a known recording location. Treat this as an experiment, not a reliable locator.

**Read results with care:** the search always returns best-fit candidates for the region
you chose - it can return a plausible-looking ranked list even when the recording is not
from that region at all. A rank-1 pin is a hypothesis to verify on the map, not a
confirmed location; the raw score has no calibrated confidence meaning.

**Privacy:** uploads are analysed in this session, then deleted (always within ~30
minutes). Only anonymous public flight, terrain and weather data is cached. Nothing is
shared. One analysis runs at a time; if the app is busy, try again in a few minutes.
"""


# ----------------------------------------------------------------- helpers

def _check_session(request: gr.Request):
    if APP_KEY and (request is None or not _hmac.compare_digest(
            str(request.cookies.get(AUTH_COOKIE, "")), AUTH_TOKEN)):
        raise gr.Error("Open your private invitation link in this browser first. No access key is needed.")


def _fmt_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(round(t)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True)
    return float(out.stdout.strip())


def _engine_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = ENGINE_SRC + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.update(extra or {})
    return env


def run_cli(args: list[str], log, timeout: int = 3600, quiet_prefixes=("|", "source:")) -> tuple[int, list[str]]:
    """Run an engine CLI subprocess, streaming its output to log. Returns (rc, output lines)."""
    proc = subprocess.Popen([sys.executable, "-m", "dopploc.cli"] + args,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=_engine_env())
    assert proc.stdout is not None
    out_lines = []
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            out_lines.append(line)
            if any(line.startswith(p) for p in quiet_prefixes):
                continue
            log(line)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise gr.Error("That step ran too long and was stopped; try a smaller region or shorter audio.")
    peak_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024
    log(f"(step exit {proc.returncode}; peak subprocess memory so far {peak_mb} MB)")
    return proc.returncode, out_lines


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
        rc, _ = run_cli(["fetch-types", "--out", str(types)], log)
        if rc != 0:
            raise gr.Error("Could not download the aircraft type table; see log.")
    log("building the regional track table (a few minutes) ...")
    rc, lines = run_cli(["ingest", day, "--raw", str(raw), "--out", str(out), "--types", str(types),
                         "--bbox", f"{bbox[0]:.5f}", f"{bbox[1]:.5f}", f"{bbox[2]:.5f}", f"{bbox[3]:.5f}",
                         "--workers", "1"], log, timeout=7200)
    if rc != 0:
        raise gr.Error("Building the regional track table failed; see log.")
    summary = {}
    for line in reversed(lines):
        try:
            summary = json.loads(line)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if summary:
        log(f"track table: {summary.get('aircraft_in_bbox', 0):,} aircraft, "
            f"{summary.get('points', 0):,} points in region")
    _trim_data()
    return out


def _fetch_url(url: str, dl_root: Path, log) -> Path:
    """Download a web video's audio track with yt-dlp (subprocess; engine stays unloaded)."""
    probe = subprocess.run(["yt-dlp", "-J", "--no-playlist", "--", url],
                           capture_output=True, text=True, timeout=300)
    if probe.returncode != 0:
        raise gr.Error("Could not download that URL. YouTube and some sites block "
                       "datacenter IPs; upload the file instead.")
    info = json.loads(probe.stdout)
    raw = f"{info.get('extractor_key', 'web')}-{info.get('id', 'x')}"
    vid = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:80]
    if len(raw) > 80:
        import hashlib
        vid += "-" + hashlib.sha1(raw.encode()).hexdigest()[:10]
    out = dl_root / vid
    out.mkdir(parents=True, exist_ok=True)
    have = sorted(p for p in out.glob("audio.*") if p.suffix not in (".part", ".ytdl", ".json"))
    if have:
        return have[0]
    title = info.get("title") or vid
    log(f"downloading audio of {title!r} ...")
    got_run = subprocess.run(["yt-dlp", "--no-playlist", "--no-progress", "-f", "bestaudio/best",
                              "-o", str(out / "audio.%(ext)s"), "--", url],
                             capture_output=True, text=True, timeout=1800)
    if got_run.returncode != 0:
        raise gr.Error("Could not download that URL. YouTube and some sites block "
                       "datacenter IPs; upload the file instead.")
    got = sorted(p for p in out.glob("audio.*") if p.suffix not in (".part", ".ytdl", ".json"))
    if not got:
        raise gr.Error("The download produced no audio track; upload the file instead.")
    return got[0]


# ----------------------------------------------------------------- step 1: detect

def do_detect(key, key_link, file_path, url, start, dur, request: gr.Request):
    _check_session(request)
    if not JOB_LOCK.acquire(blocking=False):
        raise gr.Error("Another analysis is running - try again in a few minutes.")
    job = JOBS / uuid.uuid4().hex[:12]
    job.mkdir(parents=True)
    lines = []

    def log(*a):
        lines.append(" ".join(str(x) for x in a))

    try:
        yield "\n".join(lines), None, None, None, None, None, gr.update(choices=[], value=[]), "Detection running. Wait for the result below.", gr.update(interactive=False)
        if file_path:
            src = job / ("upload" + Path(file_path).suffix)
            shutil.copy(file_path, src)
        elif url and url.strip():
            log("resolving URL ...")
            yield "\n".join(lines), None, None, None, None, None, gr.update(), "Detection running. Wait for the result below.", gr.update(interactive=False)
            src = _fetch_url(url.strip(), job / "dl", log)
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
        yield "\n".join(lines), None, None, None, None, None, gr.update(), "Detection running. Wait for the result below.", gr.update(interactive=False)
        t0 = time.time()
        outdir = job / "detect"
        rc, _ = run_cli(["detect", str(src), "--start", str(start), "--duration", str(dur),
                         "--out", str(outdir)], log)
        if rc != 0:
            raise gr.Error("Detection failed; see log.")
        log(f"detection took {time.time() - t0:.0f} s")

        rep = (outdir / "report.md").read_text()
        det = json.loads((outdir / "detections.json").read_text())
        events_doc = tomllib.loads((outdir / "events.toml").read_text()) if (outdir / "events.toml").exists() else {}
        exported = events_doc.get("event", [])
        # events.toml can omit unrefined curves and include close-range sources.
        # Pair by CPA, not by position in two differently filtered lists.
        fly = det.get("flyovers", [])
        strong, events = [], []
        for f in fly:
            if f.get("confidence") == "low" or f.get("kind") != "aircraft":
                continue
            e = next((e for e in exported
                      if abs(float(e["audio_cpa"]) - float(f["t_cpa"])) < 0.01), None)
            if e is not None:
                strong.append(f)
                events.append(e)
        choices = [f"{n}: t={_fmt_time(f['t_cpa'])} f0={f['f0']:.0f} Hz v={f['v']:.0f} m/s "
                   f"d={f['d']:.0f} m ({f['confidence']})" for n, f in enumerate(strong)]
        default = choices[:2]
        count = len(events)
        status = (f"Detection complete: {count} usable aircraft flyovers. Select exactly two, then Locate."
                  if count >= 2 else
                  f"Detection complete: {count} usable aircraft flyovers. Locate needs two. "
                  "Low-confidence curves and close-range sounds are not usable for matching. "
                  "Try a longer recording or another section with two clear propeller-aircraft passes.")
        weak = sum(f.get("kind") == "aircraft" and f.get("confidence") == "low" for f in fly)
        close = sum(f.get("kind") == "close" for f in fly)
        headline = ("Ready to try Locate" if count >= 2 else "Not enough usable aircraft passes to locate")
        explanation = ("Choose exactly two aircraft passes below, then enter the UTC date and area."
                       if count >= 2 else
                       "Detect did run successfully. Locate needs two usable aircraft passes, so it cannot continue with this result. "
                       "This does not prove there is no aircraft in the recording. Try a longer recording or another section with two clear propeller-aircraft passes.")
        readable = (f"### {headline}\n\n**{count} usable aircraft passes** \n"
                    f"{weak} weak aircraft-like candidates, excluded from locating. \n"
                    f"{close} close-range sounds (for example, vehicles), excluded.\n\n{explanation}")
        log(status)
        state = {"job": str(job), "src": str(src), "events": events,
                 "strong": strong, "status": "complete"}
        png = str(outdir / "detections.png") if (outdir / "detections.png").exists() else None
        yield ("\n".join(lines), readable, png, state, str(outdir / "events.toml"),
               str(outdir / "detections.json"), gr.update(choices=choices, value=default), status, gr.update(interactive=count >= 2))
    finally:
        JOB_LOCK.release()


# ----------------------------------------------------------------- step 2: locate

def do_locate(key, key_link, state, picks, day, lat, lon, radius_km, request: gr.Request):
    _check_session(request)
    if not state:
        raise gr.Error("No completed detection result in this session. Run Detect and wait for it to finish. "
                       "If you reloaded the page or the server restarted, the previous result has expired.")
    if len(state.get("events", [])) < 2:
        raise gr.Error("Detection completed, but found fewer than two usable aircraft flyovers. "
                       "Low-confidence curves and close-range sounds cannot be matched. "
                       "Try a longer recording or another section with two clear propeller-aircraft passes.")
    if not Path(state["src"]).exists():
        raise gr.Error("This detection has expired and its recording was deleted. Run Detect again.")
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
        os.environ["DOPPLOC_GRID_CRS"] = crs  # picked up by every engine subprocess (JOB_LOCK serializes jobs)

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
        rc, _ = run_cli(["search", str(case_toml)], log, timeout=1800,
                        quiet_prefixes=("|",))
        if rc != 0:
            raise gr.Error("Search failed; see log.")
        yield "\n".join(lines), None, None, None, None

        log(f"modelling and scoring candidates (cap {MAX_SCORE_CANDIDATES:,}) ...")
        yield "\n".join(lines), None, None, None, None
        t0 = time.time()
        proc = subprocess.run([sys.executable, str(RUNNER), str(case_toml), "15"],
                              capture_output=True, text=True, timeout=5400,
                              env=_engine_env({"DOPPLOC_GRID_CRS": crs}))
        cand_line = next((l for l in proc.stdout.splitlines() if l.startswith("CANDIDATES ")), "")
        m = re.search(r"all=(\d+) scored=(\d+) trimmed=(\w+)", cand_line)
        if proc.returncode == 3 or (m and int(m.group(1)) == 0):
            raise gr.Error("No candidate spot in this region matched both flyovers. "
                           "Try a wider region, a different day, or other detections.")
        lines += [l for l in proc.stdout.strip().splitlines()
                  if l.strip() and not l.startswith("CANDIDATES")][-8:]
        if proc.returncode != 0:
            log(proc.stderr[-1500:])
            raise gr.Error("Scoring failed; see log.")
        if m:
            n_all, n_scored, trimmed = int(m.group(1)), int(m.group(2)), m.group(3) == "True"
            log(f"scored {n_scored:,} candidate spots"
                + (f" of {n_all:,} (kept the {n_scored:,} with the tightest event timing)" if trimmed else ""))
        peak_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024
        log(f"scoring took {time.time() - t0:.0f} s (peak subprocess memory so far {peak_mb} MB)")

        with open(out / "best_per_pair.csv", newline="") as f:
            best_rows = list(csv.DictReader(f))
        top = best_rows[:25]
        rows = []
        for i, r in enumerate(top, 1):
            rlat, rlon = float(r["lat"]), float(r["lon"])
            rows.append([i, round(rlat, 5), round(rlon, 5), round(float(r["score"]), 3),
                         r["f1"].split(":")[0], r["f2"].split(":")[0],
                         f"https://www.openstreetmap.org/?mlat={rlat:.5f}&mlon={rlon:.5f}#map=16/{rlat:.5f}/{rlon:.5f}"])
        import folium
        fmap = folium.Map(location=[lat, lon], zoom_start=10)
        folium.Circle([lat, lon], radius=radius_km * 1000, color="#888", weight=1,
                      fill=False, dash_array="4").add_to(fmap)
        for i, r in enumerate(top[:15], 1):
            rlat, rlon = float(r["lat"]), float(r["lon"])
            color = "#d33" if i == 1 else ("#e83e8c" if i <= 3 else "#2b7bbb")
            folium.CircleMarker([rlat, rlon], radius=7 if i == 1 else 5, color=color,
                                fill=True, fill_opacity=0.85,
                                popup=f"#{i} score {float(r['score']):.2f}").add_to(fmap)
        map_html = fmap._repr_html_()
        summary = (out / "summary.md").read_text() if (out / "summary.md").exists() else ""
        summary = ("**Best-fit candidates, not a verified location.** The search ranks the "
                   "spots whose audible sky matches best *within the region you chose*; a wrong "
                   "region can still produce plausible-looking matches. Verify on the map before "
                   "trusting a pin.\n\n" + summary)
        rank1 = str(out / "rank1.png") if (out / "rank1.png").exists() else None
        log("done.")
        yield ("\n".join(lines), summary, rank1, map_html, rows)
    finally:
        JOB_LOCK.release()


# ----------------------------------------------------------------- UI

with gr.Blocks(title="Doppler Locate playground") as demo:
    gr.Markdown(BANNER)
    gr.Markdown("Signed in on this browser. No access key is needed.")
    key_in = gr.Textbox(visible=False)
    key_link = gr.Textbox(visible=False)
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
    detect_report = gr.Markdown()
    with gr.Accordion("Technical detection log (optional)", open=False):
        detect_log = gr.Textbox(label="progress", lines=8, interactive=False)
    detect_img = gr.Image(label="spectrogram with detected flyovers", type="filepath")
    with gr.Row():
        events_dl = gr.File(label="events.toml")
        detections_dl = gr.File(label="detections.json")
    detect_state = gr.State({})

    with gr.Group():
        gr.Markdown("### 2 - Locate the recording")
        gr.Markdown("Locate unlocks only after detection finds at least two usable aircraft flyovers. "
                    "Low-confidence curves and close-range sounds are excluded.")
        picks_in = gr.CheckboxGroup(label="two flyovers to match (from the detection above)", choices=[])
        with gr.Row():
            day_in = gr.Textbox(label="UTC date of the recording", placeholder="2026-11-21")
        with gr.Row():
            lat_in = gr.Number(label="centre latitude", value=32.08)
            lon_in = gr.Number(label="centre longitude", value=34.78)
            radius_in = gr.Slider(label="search radius (km)", minimum=10, maximum=MAX_RADIUS_KM,
                                  value=30, step=5)
        locate_btn = gr.Button("Locate", variant="primary", interactive=False)
    locate_log = gr.Textbox(label="progress", lines=10, interactive=False)
    locate_summary = gr.Markdown()
    locate_img = gr.Image(label="winning pair: modelled curves over the recording", type="filepath")
    locate_map = gr.HTML()
    locate_table = gr.Dataframe(label="top candidate spots", interactive=False,
                                headers=["rank", "lat", "lon", "score", "flight 1", "flight 2", "map"])

    detect_btn.click(do_detect, [key_in, key_link, file_in, url_in, start_in, dur_in],
                     [detect_log, detect_report, detect_img, detect_state, events_dl, detections_dl,
                      picks_in, locate_log, locate_btn])
    locate_btn.click(do_locate, [key_in, key_link, detect_state, picks_in, day_in, lat_in, lon_in, radius_in],
                     [locate_log, locate_summary, locate_img, locate_map, locate_table])

    demo.load(None, None, None, js="() => { localStorage.removeItem('dopploc_key'); if (location.hash) history.replaceState(null, '', location.pathname); }")

    gr.Markdown(
        "---\nEngine: [digitalis-p/doppler-locate](https://github.com/digitalis-p/doppler-locate) (MIT). "
        "Data: [adsb.lol](https://adsb.lol) globe history (ODbL), Copernicus GLO-90 DEM, EGM96 geoid. "
        "Runs on free community compute; not affiliated with the engine's author. "
        "Use only on your own recordings or with the consent of whoever made them.")

if __name__ == "__main__":
    import uvicorn
    demo.queue(default_concurrency_limit=4)
    gr.mount_gradio_app(fast, demo, path="/", max_file_size="150mb",
                        allowed_paths=[str(JOBS)])
    uvicorn.run(fast, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))

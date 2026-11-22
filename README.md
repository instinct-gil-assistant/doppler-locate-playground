---
title: Doppler Locate Playground
emoji: ✈️
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
---

# Doppler Locate Playground

Web playground for [digitalis-p/doppler-locate](https://github.com/digitalis-p/doppler-locate) (MIT):
find where a recording was made from propeller-aircraft Doppler curves matched against public
ADS-B flight history. Not affiliated with the engine's author.

## Status (2026-11-23)

Now hosted on Modal (serverless containers, free tier):
`modal deploy modal_app.py` builds the image (engine pinned to commit
ed84e0f, Gradio 6) and serves the app behind a small FastAPI gate
(upload/job/file routes require a session cookie; the landing page stays
public so the container cold-starts on first visit). The gate accepts either
the app key (POST /dopploc-auth, stored only in a vault) or an expiring
HMAC invite link (GET /dopploc-invite?exp=...&sig=...) that carries no
persistent secret.

Free-tier reality (measured 2026-11-23): cardless Modal workspaces are
hard-capped at $1.00 of usage per billing period ("Set by Modal", raisable
only by adding a payment method), so this is a monthly hard quota, not
unlimited free hosting. One full evening of hosted validation (a 4 GB ADS-B
day ingest, one full locate, several partial runs) consumed $0.15. Each
additional day+region ingests ~4 GB once; cached day+region searches are
minutes. Peak subprocess memory: ~2.5 GB during scoring (the Render-era
~1 GB figure was stale), so the app is configured at 2 cores / 4 GiB,
max 1 container, scale-to-zero after 15 min idle.

Validation record: locally, on real 2026-09-08 adsb.lol history with a
synthetic recording of known origin (observer 41.13017,-73.65785), the app
pipeline ranked the true flight pair #1 (~720 m error at app level; 96 m in
a 42-candidate pool variant). On the hosted Modal deployment, end-to-end
mechanics (gate, detect, URL ingest, search, scoring, map, candidate table)
all pass, but known-answer accuracy did NOT reproduce in hosted runs with
either the automatic strongest detections or generator-offset-matched
detection picks (best-fit candidates landed tens of km from the true
observer). Treat hosted geolocation output as experimental: best-fit
hypotheses to verify, never confirmed locations. The UI says so.

The earlier Render free-tier (512 MB) deployment is suspended: engine
subprocess steps peak at 480-850 MB and the Gradio server adds ~220 MB,
so any real analysis exceeds the instance limit and the instance is
OOM-killed.

Ops record: the automation account's fine-grained push token for this
repository expires 2026-12-22 (its value is stored only in a vault, never
in this repo). The Modal gate key likewise lives only in a vault and in the
Modal workspace secret `doppler-gate`.

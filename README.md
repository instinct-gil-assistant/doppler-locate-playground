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

## Status (2026-11-22)

The hosted demo on Render's free tier (512 MB RAM) is suspended. Measured on that
tier: the engine's subprocess steps peak at 480-850 MB and the Gradio server adds
~220 MB, so any real analysis exceeds the instance limit and the instance is
OOM-killed. It runs locally or on a host with about 1 GB RAM:

    docker build -t doppler-playground .
    docker run -p 7860:7860 -e DOPPLOC_APP_KEY=choose-a-key doppler-playground

Validated locally on real 2026-09-08 adsb.lol history with synthetic recordings of
known origin: top-1 error 96 m (New York area) and 140 m (northern Israel, using the
DOPPLOC_GRID_CRS azimuthal-equidistant grid patch), and the full web pipeline
(upload or media URL, detect, locate) passes end to end on adequate compute.
Candidate trimming before scoring is blind (silence filter, then tightest event
timing) and the UI always prints both the total candidate count and the number
actually scored.

Ops record: the automation account's fine-grained push token for this repository
expires 2026-12-22 (its value is stored only in a vault, never in this repo).

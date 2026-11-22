"""Modal deployment for the Doppler Locate playground.

Wraps the repo's Gradio app (app.py + score_runner.py) as a Modal web_server
function. Engine source is cloned and patched exactly like the Dockerfile.
Runs only on demand: scale-to-zero when idle, one container, one job at a time.
"""

import modal

ENGINE_COMMIT = "ed84e0f066d60833f7e9683c286a1f4838e28639"

app = modal.App("doppler-locate")

image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("ffmpeg", "git")
    .pip_install(
        f"git+https://github.com/digitalis-p/doppler-locate@{ENGINE_COMMIT}",
        "gradio>=5.0",
        "folium>=0.15",
        "requests>=2.31",
        "yt-dlp>=2025.1.1",
    )
    .run_commands(
        "git clone --depth 1 https://github.com/digitalis-p/doppler-locate /opt/doppler-locate"
        f" && cd /opt/doppler-locate && git fetch --depth 1 origin {ENGINE_COMMIT}"
        f" && git checkout {ENGINE_COMMIT}"
    )
    .add_local_file("/tmp/doppler-deploy/geo_patch.py", "/tmp/geo_patch.py", copy=True)
    .run_commands("python /tmp/geo_patch.py && rm /tmp/geo_patch.py")
    .add_local_file("/tmp/doppler-deploy/app.py", "/home/user/app/app.py", copy=True)
    .add_local_file("/tmp/doppler-deploy/score_runner.py", "/home/user/app/score_runner.py", copy=True)
    .env(
        {
            "DOPPLOC_WORK": "/tmp/doppler-web-work",
            "DOPPLOC_DATA": "/data",
            "DOPPLOC_SRC": "/opt/doppler-locate/src",
            "NUMBA_NUM_THREADS": "2",
            "GRADIO_SERVER_NAME": "0.0.0.0",
        }
    )
)

data_vol = modal.Volume.from_name("doppler-data", create_if_missing=True)


@app.function(
    image=image,
    cpu=2.0,
    memory=4096,
    timeout=43200,
    scaledown_window=900,
    max_containers=1,
    volumes={"/data": data_vol},
    secrets=[modal.Secret.from_name("doppler-gate")],
)
@modal.concurrent(max_inputs=32)
@modal.web_server(7860, startup_timeout=180)
def web():
    import subprocess

    subprocess.Popen(["python", "/home/user/app/app.py"])

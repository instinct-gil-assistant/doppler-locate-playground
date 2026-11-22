FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/user/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# engine source, patched to allow a non-CONUS search grid via DOPPLOC_GRID_CRS
RUN git clone --depth 1 https://github.com/digitalis-p/doppler-locate /opt/doppler-locate \
    && cd /opt/doppler-locate && git fetch --depth 1 origin ed84e0f066d60833f7e9683c286a1f4838e28639 \
    && git checkout ed84e0f066d60833f7e9683c286a1f4838e28639
COPY geo_patch.py /tmp/geo_patch.py
RUN python /tmp/geo_patch.py && rm /tmp/geo_patch.py

COPY app.py .

ENV DOPPLOC_WORK=/tmp/doppler-web-work \
    DOPPLOC_DATA=/tmp/doppler-data \
    DOPPLOC_SRC=/opt/doppler-locate/src \
    NUMBA_NUM_THREADS=2 \
    GRADIO_SERVER_NAME=0.0.0.0

EXPOSE 7860
CMD ["python", "app.py"]

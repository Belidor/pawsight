# The "brain" image: zones, heuristics, bowl visits, VLM orchestration,
# SQLite and Telegram. There is NO torch, ultralytics or OpenCV here:
# only the detector needs them, and it has to be native for Metal.
# That is why the image is tens of megabytes, not gigabytes.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# tzdata is needed for the TZ variable to actually work
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# cryptography: for the local miIO protocol (camera pan/tilt and auto-follow).
# We do NOT use python-miio: it pulls in netifaces, which cannot be built without
# a compiler, and pawsight/miio_lite.py implements the part of the protocol we need
# (checked against python-miio).
RUN pip install --no-cache-dir \
        "paho-mqtt>=2.0" \
        "httpx>=0.27" \
        "pyyaml>=6" \
        "python-dotenv>=1" \
        "aiohttp>=3.9" \
        "cryptography>=42"

COPY pawsight/ /app/pawsight/
COPY zones.example.yaml /app/zones.yaml

# Non-root user: the container needs no privileges.
RUN useradd -u 10001 -m pawsight && mkdir -p /app/storage && chown -R pawsight /app
USER pawsight

EXPOSE 8099
CMD ["python", "-m", "pawsight", "brain"]

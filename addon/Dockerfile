ARG BUILD_FROM=python:3.12-slim-bookworm
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 SWIMWATCH_DATA=/data HF_HOME=/data/models/hf \
    HF_HUB_DISABLE_TELEMETRY=1 TRANSFORMERS_VERBOSITY=error HF_HUB_DISABLE_PROGRESS_BARS=1

RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/requirements.txt .
# CPU-only torch: the normal wheels bring several GB of NVIDIA libraries.
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt \
    && pip list 2>/dev/null | grep -i nvidia && exit 1 || true

COPY app/swimwatch ./swimwatch

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=dev
LABEL io.hass.type="addon" io.hass.name="SwimWatch" io.hass.arch="${BUILD_ARCH}" io.hass.version="${BUILD_VERSION}" \
      org.opencontainers.image.source="https://github.com/frazz1234/swimwatch" \
      org.opencontainers.image.description="SwimWatch Home Assistant add-on" \
      org.opencontainers.image.licenses="MIT"
CMD ["python", "-m", "swimwatch"]

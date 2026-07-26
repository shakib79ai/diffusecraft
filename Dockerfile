# Container image for the DiffuseCraft demo app (app.py).
#
# This packages the OSS Gradio demo for container deployment (e.g. as the base image
# for the ECS GPU worker in docs/PRODUCTION_ARCHITECTURE.md, or for local `docker run`
# on a CUDA-capable host). It is NOT the production FastAPI service described in
# docs/API_REFERENCE.md -- that service wraps the same model-loading core but swaps
# the Gradio UI for the REST API contract, and would use this Dockerfile as a starting
# point rather than its final form.

FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 7860

# Liveness check hits Gradio's own health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:7860/', timeout=3)" || exit 1

CMD ["python3", "app.py"]

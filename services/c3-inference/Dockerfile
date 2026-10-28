# C3 SAM2 Inference
#
# Base: PyTorch 2.7.1 + CUDA 12.8 runtime. CUDA 12.8 wheels include Blackwell
# (RTX 50xx, sm_120) kernels and still run on older GPUs (driver >= 570) and on CPU.
# Model checkpoints are NOT baked in: ./models is mounted at /app/models (read-only).
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime

# SAM2 pinned to a commit of https://github.com/facebookresearch/sam2 (main, 2026-10-28).
# The SAM2 predictor backend uses SAM2ImagePredictor internals, so bump deliberately.
ARG SAM2_REF=2b90b9f5ceec907a1c18123530e92e794ad901a4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Skip SAM2's optional CUDA extension (needs nvcc; only used for mask hole filling).
    SAM2_BUILD_CUDA=0

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && pip install --no-build-isolation --no-deps \
        "SAM-2 @ https://github.com/facebookresearch/sam2/archive/${SAM2_REF}.zip" \
    && python -c "import torch, sam2.sam2_image_predictor; print('torch', torch.__version__)"

COPY src/ ./src/

RUN mkdir -p /app/models
VOLUME ["/app/models"]

EXPOSE 8002

# 200 only once the model is loaded and warmed up (see GET /healthz).
HEALTHCHECK --interval=10s --timeout=3s --start-period=120s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('C3_PORT','8002'), timeout=2)" || exit 1

CMD ["python", "-m", "src"]

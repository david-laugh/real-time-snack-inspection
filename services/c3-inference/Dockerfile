# C3 SAM2 Inference — skeleton
#
# ============================================================================
# TODO(Member C): GPU / SAM2 environment
#   1. Replace the base image with a PyTorch + CUDA image, e.g.
#        FROM pytorch/pytorch:<torch>-cuda<ver>-cudnn<ver>-runtime
#      Match the CUDA version to the host NVIDIA driver.
#   2. Install SAM2 (https://github.com/facebookresearch/sam2), e.g.
#        RUN git clone https://github.com/facebookresearch/sam2.git /opt/sam2 \
#            && pip install -e /opt/sam2
#   3. Do NOT download or COPY model checkpoints into the image.
#      Checkpoints are mounted at runtime: ./models -> /app/models (read-only).
#   4. Enable GPU reservation in docker-compose.yml (deploy.resources.devices).
# ============================================================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Model weights mount point (not baked into the image)
RUN mkdir -p /app/models
VOLUME ["/app/models"]

EXPOSE 8002

# TODO(Member C): replace with real inference server entrypoint.
CMD ["sleep", "infinity"]

# C4 Visualizer — skeleton
# Future: real-time overlay (mask/bbox/defect marking) + FPS/latency/defect/drop dashboard.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

EXPOSE 8003

# TODO(Member D): replace with real entrypoint.
CMD ["sleep", "infinity"]

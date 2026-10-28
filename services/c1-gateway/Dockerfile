# C1 Gateway — skeleton
# Future: FastAPI WebSocket server receiving 30fps stream from smartphone/simulator.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

EXPOSE 8000

# TODO(Member A): replace with real entrypoint, e.g.
# CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
CMD ["sleep", "infinity"]

# C2 Preprocessing — OpenCV resize / candidate bbox detection -> SAM2 box prompts.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# opencv-python-headless wheels bundle their own libs; no extra system packages needed.

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY schemas/ ./schemas/
COPY src/ ./src/

EXPOSE 8001

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=2)"

CMD ["uvicorn", "c2.app:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8001"]

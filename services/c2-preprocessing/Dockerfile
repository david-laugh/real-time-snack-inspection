# C2 Preprocessing — skeleton
# Future: OpenCV resize / ROI crop / candidate bbox generation -> SAM2 prompts.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# TODO(Member B): add system libs if non-headless OpenCV is needed (e.g. libgl1, libglib2.0-0).

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

EXPOSE 8001

# TODO(Member B): replace with real entrypoint.
CMD ["sleep", "infinity"]

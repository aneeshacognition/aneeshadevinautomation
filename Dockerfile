FROM python:3.11-slim

# Avoid interactive prompts and keep Python output unbuffered so container
# logs stream in real time.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dependabot_scanner.py .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 scanner
USER scanner

ENTRYPOINT ["python", "dependabot_scanner.py"]

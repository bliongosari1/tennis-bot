# Base image ships with Chromium + all OS deps pre-installed.
# Pinned to a recent version that matches our requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

# Reduce image size: don't keep pip cache.
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project.  Skip files that are useless inside the
# container (caches, .git, local logs, secrets) via .dockerignore.
COPY . /app/

# Default command — single Telegram + cron loop.
CMD ["python", "fly_main.py"]

# Cloud Crucix Community Edition - read-only BigQuery activity dashboard, local web app.
FROM python:3.11-slim-bookworm

# Apply OS security patches and clear the apt cache to keep the image small.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge.py analysis.py reporting.py config.yaml ui.html VERSION.txt /app/

# Mount points: the service-account key, and settings saved from the UI.
RUN mkdir -p /app/secrets /app/output
ENV CRUCIX_CONFIG_DIR=/app/secrets

# BIND_HOST is 0.0.0.0 so Docker can forward the port; the host publishes it to
# ITS OWN 127.0.0.1, so the dashboard stays reachable only from this machine.
ENV BIND_HOST=0.0.0.0
ENV PORT=5006
EXPOSE 5006

ENTRYPOINT ["python", "bridge.py"]

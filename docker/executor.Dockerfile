FROM python:3.11-slim

# Install only essential system packages (no git)
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Python packages for the sandbox runtime
COPY docker/requirements-docker.txt /app/requirements-docker.txt
RUN pip install --no-cache-dir -r /app/requirements-docker.txt

# User site-packages enabled for pip install --user support
RUN useradd -m -u 1000 agent && mkdir /workspace && chown agent:agent /workspace
WORKDIR /workspace
USER agent

CMD ["tail", "-f", "/dev/null"]
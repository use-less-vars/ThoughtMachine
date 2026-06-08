FROM python:3.11-slim

# Install only essential system packages (no git)
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Minimal Python packages for the sandbox runtime (no ML, no AI)
RUN pip install --no-cache-dir \
    pydantic \
    pytest \
    fastapi \
    uvicorn \
    websockets \
    orjson \
    docker \
    PyYAML \
    pathspec \
    python-dotenv

# User site-packages enabled for pip install --user support
RUN useradd -m -u 1000 agent && mkdir /workspace && chown agent:agent /workspace
WORKDIR /workspace
USER agent

CMD ["tail", "-f", "/dev/null"]
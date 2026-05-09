FROM python:3.11-slim

# Install git and requirements system-wide
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pydantic libcst pytest openai tiktoken

# Disable user site-packages (writable dirs have noexec, preventing .so loading)
ENV PYTHONNOUSERSITE=1

RUN useradd -m -u 1000 agent && mkdir /workspace && chown agent:agent /workspace
WORKDIR /workspace
USER agent

CMD ["tail", "-f", "/dev/null"]

FROM python:3.11-slim

# Install requirements system-wide (no git — security: prevents git:write privilege escalation via DockerCodeRunner)
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pydantic libcst pytest openai tiktoken fastapi uvicorn websockets orjson anthropic docker PyYAML sseclient beautifulsoup4 lxml sentence-transformers chromadb langchain langchain-community pathspec tree-sitter fast-json-repair modelcontextprotocol mcp python-dotenv

# User site-packages enabled for pip install --user support
# ENV PYTHONNOUSERSITE=1  # removed: prevents .so loading in writable dirs

RUN useradd -m -u 1000 agent && mkdir /workspace && chown agent:agent /workspace
WORKDIR /workspace
USER agent

CMD ["tail", "-f", "/dev/null"]

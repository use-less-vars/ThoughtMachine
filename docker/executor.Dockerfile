# Use a slim Python base
FROM python:3.11-slim

# Copy requirements first (for better layer caching)
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Create a non-root user (UID 1000, can be changed)
RUN useradd -m -u 1000 agent && \
    mkdir /workspace && chown agent:agent /workspace

# Set working directory
WORKDIR /workspace

# Switch to non-root user
USER agent

# Keep container alive (default command)
CMD ["tail", "-f", "/dev/null"]
# Use a slim Python base
FROM python:3.11-slim

# Create a non-root user (UID 1000, can be changed)
RUN useradd -m -u 1000 agent && \
    mkdir /workspace && chown agent:agent /workspace

# Set working directory
WORKDIR /workspace

# Switch to non-root user
USER agent

# Keep container alive (default command)
CMD ["tail", "-f", "/dev/null"]
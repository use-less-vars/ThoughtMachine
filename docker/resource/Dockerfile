# docker/resource/Dockerfile — minimal git sandbox for the hidden resource container.
#
# Build: docker build -f docker/resource/Dockerfile -t tm-resource-git docker/resource
#
# Security intent: the SMALLEST image that can run git against the real
# workspace. No docker CLI, no docker socket, no python packages, no vault.
# The container is launched by ResourceContainerManager with a read-only
# rootfs, all capabilities dropped, no-new-privileges, no network, and only
# the workspace bind mount (rw, since git writes .git metadata).

# Layer 1: base. python:3.12-slim is ~120MB compressed; we inherit python3
# (used by the sandbox test's network-egress fallback check) without adding
# a separate runtime.
FROM python:3.12-slim

# Layer 2: git + CA certificates. --no-install-recommends keeps it small;
# ca-certificates is required for https remotes (git:// and ssh remotes would
# need openssh-client — deliberately omitted to shrink the image).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Layer 3: non-root user 'agent' with UID 1000 (must match the
# user="1000:1000" the manager passes to containers.run — Docker resolves
# "1000:1000" numerically, so the uid/gid, not the name, is what matters).
# -m creates /home/agent (the manager mounts tmpfs there, writable).
# Pre-create /home/agent/.local owned by uid 1000: the tm-packages named
# volume mounts there at runtime, and a fresh volume root inherits the image
# dir's ownership, so pip --user installs work as the agent user.
RUN groupadd -g 1000 agent \
    && useradd -m -u 1000 -g 1000 -s /bin/bash agent \
    && mkdir -p /home/agent/.local \
    && chown 1000:1000 /home/agent/.local

# Layer 4: HOME for git's global config (~/.gitconfig lives on the /home/agent
# tmpfs, so `git config --global safe.directory ...` works and persists only
# for the container's lifetime).
ENV HOME=/home/agent

# Layer 5: pre-create /workspace owned by root. It is shadowed at runtime by
# the host workspace bind mount, but docker needs the image path to exist when
# the bind target is empty/absent on the host. Must run BEFORE `USER agent`:
# / is only writable by root in the image, so a filesystem-root mkdir needs
# root privileges (as the agent user it would fail with EACCES).
RUN mkdir -p /workspace

# Layer 6: run as the non-root agent. WORKDIR below stays valid after this
# switch because /workspace already exists in the image (and the runtime bind
# mount shadows it anyway).
USER agent

WORKDIR /workspace

# Layer 7: keep the container alive after `containers.run(detach=True)` so
# subsequent exec_run calls have a running target (same pattern as the
# executor's `tail -f /dev/null`).
CMD ["tail", "-f", "/dev/null"]

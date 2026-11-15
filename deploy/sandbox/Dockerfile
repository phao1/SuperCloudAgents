# Sandbox image: what an agent needs to work on a repository.
# The agent binary itself is mounted in at runtime by the agent backend.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl build-essential ripgrep \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pytest \
 && git config --system --add safe.directory '*' \
 && mkdir -p /workspace /agent_home && chmod 1777 /workspace /agent_home /tmp
ENV HOME=/agent_home PIP_USER=1 PATH=/agent_home/.local/bin:$PATH
WORKDIR /workspace
CMD ["sleep", "infinity"]

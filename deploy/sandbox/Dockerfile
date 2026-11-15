# Sandbox image: what an agent needs to work on a repository.
# Optional image containing /opt/codex/codex for sync deployments.
ARG CODEX_IMAGE=python:3.12-slim
FROM ${CODEX_IMAGE} AS codex_source
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl build-essential ripgrep \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pytest \
 && git config --system --add safe.directory '*' \
 && mkdir -p /workspace /agent_home && chmod 1777 /workspace /agent_home /tmp
COPY --from=codex_source /opt/ /opt/
ENV HOME=/agent_home PIP_USER=1 PATH=/agent_home/.local/bin:$PATH
WORKDIR /workspace
CMD ["sleep", "infinity"]

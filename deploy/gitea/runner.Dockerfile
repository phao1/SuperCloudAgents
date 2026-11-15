# act_runner in host mode: jobs run inside this container, so it carries the tools the
# example pipeline needs. No Docker socket is mounted.
FROM gitea/act_runner:0.2.11
RUN apk add --no-cache bash git curl python3 py3-pytest

FROM node:22.22.2-bookworm-slim AS node
FROM sca-sandbox:latest
USER root
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
ARG DSH_VERSION=0.2.2-rc.5
RUN npm install --global --omit=dev --no-audit --no-fund @deepseek-ai/dsh@${DSH_VERSION} \
    && npm cache clean --force

FROM sca-sandbox:latest
USER root
COPY bun /usr/local/bin/bun
RUN echo '10702245766b225e70bbf6235fd6b47214fd94b4760d00ffaed8ab0df57ac235  /usr/local/bin/bun' | sha256sum -c - \
    && chmod 755 /usr/local/bin/bun \
    && test "$(bun --version)" = '1.4.4'
# Dependencies are mounted read-only from the trusted host cache at task creation.

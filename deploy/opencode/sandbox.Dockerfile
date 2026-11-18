FROM sca-sandbox:latest
USER root
COPY bun /usr/local/bin/bun
# Bun 1.4.4 from oven/bun:1.4.4, pinned per architecture.
RUN case "$(dpkg --print-architecture)" in \
      amd64) sum=10702245766b225e70bbf6235fd6b47214fd94b4760d00ffaed8ab0df57ac235 ;; \
      arm64) sum=354c2b049c716aca07b2e5cddb887928ac58950675d1258bb6312083917793b6 ;; \
      *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac \
    && echo "$sum  /usr/local/bin/bun" | sha256sum -c - \
    && chmod 755 /usr/local/bin/bun \
    && test "$(bun --version)" = '1.4.4'
# Dependencies are mounted read-only from the trusted host cache at task creation.

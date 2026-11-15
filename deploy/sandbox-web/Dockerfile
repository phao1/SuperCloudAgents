FROM sca-sandbox:latest
USER root
ENV PIP_USER=0 PLAYWRIGHT_BROWSERS_PATH=/opt/sca-web/browsers
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm bzip2 \
 && mkdir -p /opt/sca-web /usr/local/share/fonts/dejavu \
 && curl -fsSL https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.tar.bz2 -o /tmp/fonts.tar.bz2 \
 && tar -xjf /tmp/fonts.tar.bz2 -C /tmp \
 && cp /tmp/dejavu-fonts-ttf-2.37/ttf/*.ttf /usr/local/share/fonts/dejavu/ \
 && cd /opt/sca-web \
 && npm install --save-exact @playwright/test@1.56.1 \
 && npx playwright install --with-deps chromium \
 && fc-cache -f \
 && pip install --no-cache-dir schemathesis==4.6.0 ruff==0.14.4 \
 && rm -rf /var/lib/apt/lists/* /tmp/fonts.tar.bz2 /tmp/dejavu-fonts-ttf-2.37
ENV PIP_USER=1

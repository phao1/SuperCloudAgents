FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends squid-openssl openssl ca-certificates python3 util-linux \
 && rm -rf /var/lib/apt/lists/*
COPY check-sni /usr/local/bin/check-sni
COPY entrypoint.sh /usr/local/bin/sca-egress
RUN chmod 755 /usr/local/bin/check-sni /usr/local/bin/sca-egress
ENTRYPOINT ["/usr/local/bin/sca-egress"]

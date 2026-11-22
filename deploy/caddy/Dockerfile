# Caddy for pmsbridge: the official image, run as the fixed non-root user 10001.
#
# Pinned by tag and digest, like the application image. To update: pull the new tag and copy its
# index digest here.
FROM caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0

# Certificates, the certificate account and the saved configuration live in /data and /config. The
# compose file mounts named volumes there, and a new named volume inherits this ownership on first
# use, so the unprivileged user can write to them.
#
# The binary's file capability is removed: ports 80 and 443 are opened without it (the compose file
# lowers the unprivileged port start for this container) and the container drops all capabilities.
RUN setcap -r /usr/bin/caddy \
    && mkdir -p /data /config \
    && chown -R 10001:10001 /data /config \
    && chmod -R u=rwX,g=,o= /data /config

USER 10001:10001

LABEL org.opencontainers.image.title="pmsbridge-caddy" \
      org.opencontainers.image.source="https://github.com/faridmostafaei259/pmsbridge" \
      org.opencontainers.image.licenses="Apache-2.0"

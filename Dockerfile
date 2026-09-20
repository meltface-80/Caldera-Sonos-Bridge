FROM python:3.12-slim

LABEL org.opencontainers.image.title="Caldera Sonos Bridge" \
      org.opencontainers.image.description="Plays Plex music to Sonos speakers by presenting each room as a Plex player" \
      org.opencontainers.image.source="https://github.com/meltface-80/Caldera-Sonos-Bridge" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HTTP_PORT=32700 \
    PLAYER_PORT_BASE=32701 \
    CONFIG_DIR=/config

WORKDIR /app

# A Plex server on your own network is reached over plain HTTP, but a server
# that insists on secure connections is reached over TLS against a real
# certificate - which needs a trust store to check it against.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY calderabridge ./calderabridge

# The config volume holds the Plex token, the saved settings and the port
# assignments, so it has to be writable by the user the bridge runs as.  Creating
# it here, owned by that user, means a fresh named volume inherits the ownership
# and works with no setup at all.  A bind-mounted host directory does not - chown
# it to 10001:10001, or run the container with --user.
RUN useradd --system --uid 10001 --no-create-home bridge \
    && mkdir -p /config \
    && chown bridge:bridge /config
VOLUME ["/config"]
USER bridge

# Informational only: the bridge needs host networking for GDM multicast and for
# Sonos discovery, so published ports do not apply in normal use.
EXPOSE 32700/tcp
EXPOSE 32701-32720/tcp
EXPOSE 32412/udp

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('HTTP_PORT','32700')+'/status.json', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "calderabridge"]

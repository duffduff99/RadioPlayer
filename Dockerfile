# RadioPlayer: one image, run as two containers (see compose.yaml):
#   engine  - Liquidsoap audio engine (needs the sound card)
#   bridge  - Home Assistant MQTT bridge
#
# Debian 13 "Trixie" ships Liquidsoap 2.3.2, the version this was tested
# against most. Builds for amd64, arm64 and armhf (Raspberry Pi 2/3/4/5).
ARG BASE=debian:trixie-slim
FROM ${BASE}

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      liquidsoap python3 python3-yaml python3-paho-mqtt ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Refuse to build with a Liquidsoap that is too old for radio.liq.
RUN v=$(liquidsoap --version | head -1 | sed -E 's/^Liquidsoap ([0-9.]+).*/\1/') \
 && echo "Liquidsoap $v" \
 && [ "$(printf '%s\n2.2\n' "$v" | sort -V | head -1)" = "2.2" ] \
    || { echo "Liquidsoap $v is too old (need >= 2.2); use a newer BASE image"; exit 1; }

# Pre-build Liquidsoap's standard-library cache (as root, into the system
# cache dir) so the engine starts in seconds instead of re-type-checking the
# whole library on every start, which takes ~20 s on a Raspberry Pi 3.
RUN liquidsoap --cache-stdlib

RUN useradd --system --no-create-home --home-dir /var/lib/radioplayer \
      --shell /usr/sbin/nologin --groups audio radioplayer \
 && install -d -o radioplayer -g radioplayer -m 0750 /var/lib/radioplayer /var/lib/radioplayer/emergency \
 && install -d -o radioplayer -g radioplayer /etc/radioplayer \
 && install -d /mnt/radio \
 && printf '#!/bin/sh\nexec python3 -m radioplayer "$@"\n' > /usr/local/bin/radioplayer \
 && chmod 0755 /usr/local/bin/radioplayer

COPY radioplayer /opt/radioplayer/radioplayer
COPY liquidsoap /opt/radioplayer/liquidsoap
COPY config/config.example.yaml /opt/radioplayer/config/config.example.yaml
COPY docker/entrypoint.sh /usr/local/bin/entrypoint

ENV PYTHONPATH=/opt/radioplayer \
    PYTHONUNBUFFERED=1

VOLUME /var/lib/radioplayer /etc/radioplayer
USER radioplayer
ENTRYPOINT ["entrypoint"]
CMD ["engine"]

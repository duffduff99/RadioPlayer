#!/bin/sh
# Container entrypoint: "engine", "bridge", or any other command.
set -e

config=/etc/radioplayer/config.yaml

case "$1" in
  engine)
    if [ ! -f "$config" ]; then
      echo "Missing $config. Copy config.example.yaml to config.yaml next to compose.yaml." >&2
      exit 1
    fi
    # Regenerate settings from config.yaml. A broken config keeps the last
    # good settings so the station stays on air.
    if ! radioplayer generate; then
      echo "WARNING: config.yaml is invalid; starting with the last good settings" >&2
    fi
    if [ ! -f /var/lib/radioplayer/generated.liq ]; then
      echo "No usable settings yet; fix config.yaml" >&2
      exit 1
    fi
    exec liquidsoap /opt/radioplayer/liquidsoap/radio.liq
    ;;
  bridge)
    exec radioplayer bridge
    ;;
  *)
    exec "$@"
    ;;
esac

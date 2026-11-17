#!/bin/sh
# Container entrypoint: "engine", "bridge", or any other command.
set -e

config=/etc/radioplayer/config.yaml

# First start: put the documented default config in the config volume, so
# there is a file to edit. Settings passed as RADIOPLAYER__* environment
# variables apply on top of it.
if [ ! -f "$config" ]; then
  tmp="$config.$$.tmp"
  if cp /opt/radioplayer/config/config.example.yaml "$tmp" 2>/dev/null; then
    # ln fails if the other container created the file first; that is fine.
    ln "$tmp" "$config" 2>/dev/null && echo "Created default $config"
    rm -f "$tmp"
  else
    echo "Missing $config, and /etc/radioplayer is not writable to create it." >&2
    exit 1
  fi
fi

case "$1" in
  engine)
    # Regenerate settings from config.yaml. A broken config keeps the last
    # good settings so the station stays on air.
    if ! radioplayer generate; then
      echo "WARNING: the configuration is invalid; starting with the last good settings" >&2
    fi
    if [ ! -f /var/lib/radioplayer/generated.liq ]; then
      echo "No usable settings yet; fix the configuration" >&2
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

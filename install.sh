#!/bin/bash
# RadioPlayer installer for Raspberry Pi OS / Debian.
#
#   sudo ./install.sh
#
# Safe to re-run: it upgrades the code and keeps your config, credentials
# and mount settings. Non-interactive use: set SMB_HOST, SMB_SHARE,
# SMB_USER, SMB_PASS (and optionally SMB_VERS, default 3.0) in the
# environment.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/opt/radioplayer
ETC=/etc/radioplayer
STATE=/var/lib/radioplayer
USER_NAME=radioplayer
MIN_LIQUIDSOAP=2.2

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0"

version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }

say "Checking Liquidsoap version available from apt"
apt-get update -qq
candidate=$(apt-cache policy liquidsoap | awk '/Candidate:/ {print $2}')
installed=$(dpkg-query -W -f='${Version}' liquidsoap 2>/dev/null || true)
best=${installed:-$candidate}
if [ -z "$best" ] || [ "$best" = "(none)" ]; then
  die "no liquidsoap package found in apt"
fi
if ! version_ge "${best%%-*}" "$MIN_LIQUIDSOAP"; then
  die "liquidsoap $best is too old (need >= $MIN_LIQUIDSOAP).
Raspberry Pi OS / Debian 12 'Bookworm' ships 2.1.3. Use Raspberry Pi OS
based on Debian 13 'Trixie' (ships 2.3.x), or install a newer .deb from
https://github.com/savonet/liquidsoap/releases and re-run this script."
fi
echo "liquidsoap $best OK"

say "Installing packages"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  liquidsoap cifs-utils alsa-utils python3-venv python3-pip sudo rsync

say "Checking that Liquidsoap starts"
if ! timeout 120 liquidsoap --version >/dev/null 2>&1; then
  avcodec=$(dpkg-query -W -f='${Version}' 'libavcodec[0-9]*' 2>/dev/null | head -1)
  case "$avcodec" in
    *rpt*)
      die "liquidsoap crashes on startup on this system.
Raspberry Pi OS replaces Debian's FFmpeg libraries with its own build
(libavcodec $avcodec), and Debian's Liquidsoap package crashes when it loads
them. Use the Docker install instead (README, section 2, option A): the
image uses Debian's own FFmpeg." ;;
    *)
      die "liquidsoap crashes on startup on this system (try: liquidsoap --version).
Use the Docker install instead (README, section 2, option A)." ;;
  esac
fi

say "Creating system user '$USER_NAME'"
if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin \
    --groups audio "$USER_NAME"
fi
usermod -aG audio "$USER_NAME"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 0750 "$STATE" "$STATE/emergency"

say "Installing code to $PREFIX"
install -d "$PREFIX"
rsync -a --delete --exclude .git --exclude venv --exclude '__pycache__' "$SRC"/ "$PREFIX"/
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q "$PREFIX"

say "Configuration in $ETC"
install -d -m 0750 -o root -g "$USER_NAME" "$ETC"
if [ ! -f "$ETC/config.yaml" ]; then
  install -m 0640 -o root -g "$USER_NAME" "$PREFIX/config/config.example.yaml" "$ETC/config.yaml"
  echo "Created $ETC/config.yaml - edit the mqtt section and output device."
else
  echo "Keeping existing $ETC/config.yaml"
fi

say "SMB share"
if [ ! -f /etc/systemd/system/mnt-radio.mount ]; then
  : "${SMB_HOST:=}" "${SMB_SHARE:=}" "${SMB_USER:=}" "${SMB_PASS:=}" "${SMB_VERS:=3.0}"
  [ -n "$SMB_HOST" ] || read -rp "SMB server (hostname or IP): " SMB_HOST
  [ -n "$SMB_SHARE" ] || read -rp "Share name (e.g. Music): " SMB_SHARE
  [ -n "$SMB_USER" ] || read -rp "SMB username: " SMB_USER
  [ -n "$SMB_PASS" ] || { read -rsp "SMB password: " SMB_PASS; echo; }
  umask 077
  printf 'username=%s\npassword=%s\n' "$SMB_USER" "$SMB_PASS" > "$ETC/smb-credentials"
  chown root:root "$ETC/smb-credentials"
  chmod 0600 "$ETC/smb-credentials"
  umask 022
  sed -e "s|@SMB_HOST@|$SMB_HOST|" -e "s|@SMB_SHARE@|$SMB_SHARE|" -e "s|@SMB_VERS@|$SMB_VERS|" \
    "$PREFIX/systemd/mnt-radio.mount.in" > /etc/systemd/system/mnt-radio.mount
else
  echo "Keeping existing /etc/systemd/system/mnt-radio.mount"
fi
install -d /mnt/radio
install -m 0644 "$PREFIX/systemd/mnt-radio.automount" /etc/systemd/system/

# Older versions used a sudoers rule and a timer; both are now handled by the
# engine and bridge themselves.
rm -f /etc/sudoers.d/radioplayer
if systemctl list-unit-files radioplayer-emergency.timer >/dev/null 2>&1; then
  systemctl disable --now radioplayer-emergency.timer 2>/dev/null || true
  rm -f /etc/systemd/system/radioplayer-emergency.timer /etc/systemd/system/radioplayer-emergency.service
fi

say "Installing services"
for unit in radioplayer-engine.service radioplayer-bridge.service; do
  install -m 0644 "$PREFIX/systemd/$unit" /etc/systemd/system/
done
systemctl daemon-reload
systemctl enable --now mnt-radio.automount
systemctl enable radioplayer-engine.service radioplayer-bridge.service

say "Checking the share"
if timeout 30 ls /mnt/radio >/dev/null 2>&1; then
  echo "Share mounted. Playlists:"
  sudo -u "$USER_NAME" "$PREFIX/venv/bin/radioplayer" check || true
else
  echo "WARNING: could not list /mnt/radio. Check: systemctl status mnt-radio.mount"
  echo "The player will still run from the emergency folder (if it has music)."
fi

say "Starting"
systemctl restart radioplayer-engine.service radioplayer-bridge.service
sleep 2
systemctl --no-pager --lines=0 status radioplayer-engine.service radioplayer-bridge.service || true

cat <<EOF

Done. Next steps:
  1. Edit $ETC/config.yaml (mqtt host/user/password, output device),
     then: sudo systemctl restart radioplayer-engine radioplayer-bridge
  2. Calibrate levels into the modulator: see "Calibration" in $PREFIX/README.md
  3. Logs: journalctl -u radioplayer-engine -u radioplayer-bridge -f
EOF

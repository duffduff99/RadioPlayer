# RadioPlayer

Always-on music playout for a Raspberry Pi feeding a Blonder Tongue ZFMSM
(or any line-level FM modulator). It plays shuffled folders from an SMB share,
processes the audio for FM, and is controlled from Home Assistant.

- **Playlists are folders.** Every top-level folder on the share becomes a
  playlist automatically. You can also define playlists that combine folders.
  Everything shuffles, and no song repeats until the whole folder has played.
- **Home Assistant control** over MQTT (auto-discovered): playlist, skip,
  output level, processing preset, now playing, and a "playing fallback" alarm.
- **Never silent.** The selected playlist falls back to the whole library,
  then to a local emergency folder on the SD card. A watchdog restarts the
  engine if the output ever goes dead while music is coming in, and systemd
  restarts anything that crashes.
- **FM processing:** slow AGC, EQ, 4-band compressor, limiter, 15 kHz
  low-pass (protects the 19 kHz stereo pilot), pre-emphasis protection so
  bright songs don't over-deviate the transmitter, and a hard ceiling no
  sample can exceed.
- **Output level control** that cannot overdrive the modulator. The level
  is applied after the limiter and capped at a maximum you set.

```
 SMB share ──► Liquidsoap engine ─────────────────────────────► 3.5 mm / USB DAC ──► ZFMSM L/R
 (/mnt/radio)   playlists → fallback → AGC → EQ → multiband
                → limiter → 15 kHz LPF → ceiling clip → level
                     ▲ HTTP (localhost)
                     │
               radioplayer bridge ◄──── MQTT ────► Home Assistant
```

---

## 1. Hardware

### Which Raspberry Pi

The audio engine (Liquidsoap) runs its processing on a single CPU core, so
single-core speed is what matters. **Measured on a real Pi 3B** (Liquidsoap
2.3, Docker), plus estimates scaled from desktop measurements:

| `processing.mode` | What runs | Pi 3B, share of one core |
|---|---|---|
| `full` | AGC, EQ, 4-band compressor, limiter, protection | **~105% (measured): too slow, audio stutters** |
| `lite` | AGC, bass/presence EQ, 1-band compressor, limiter, protection | ~65–70% (est.) |
| `minimal` | limiter, 15 kHz filter, pre-emphasis protection, ceiling | ~55% (est.) |

"Protection" means everything that keeps the transmitter safe: limiter,
15 kHz low-pass, pre-emphasis protection and the hard ceiling. It's in every
mode. A Pi 4 is roughly 2–3× faster per core than a Pi 3B and should run
`full` comfortably; a Pi 5 more so.

If the engine log shows `Underrun!` or `catchup`, or the audio stutters,
step down a mode. Check the load with `docker stats --no-stream` (Docker) or
`top -p $(pgrep liquidsoap)` (native).

The engine uses about 170 MB of RAM, so 1 GB boards are fine.

### Audio output

- **USB DAC (recommended).** The Pi's 3.5 mm jack is a PWM output with an
  audible noise floor. The ZFMSM's own S/N is >60 dB, and the jack's hiss
  will be audible in quiet passages. Any class-compliant USB DAC works
  (including cheap USB-C/USB-A dongles).
- **Built-in 3.5 mm jack** works if that's what you have. Set `output.device`
  as shown in the config.

### Connecting to the ZFMSM

From the spec sheet: **audio inputs are F-connectors (L and R), max input
2.0 Vp-p, audio range 30 Hz – 15 kHz.** You need a 3.5 mm stereo → 2× RCA
cable plus two RCA-female → F-male adapters.

If you hear hum, it's usually a ground loop between the Pi's power supply
and the headend. Fix it with a stereo ground-loop isolator on the audio
cable.

---

## 2. Install

Two ways to install, with the same features:

- **Docker (recommended).** Works on any Linux machine with Docker: a
  Raspberry Pi (any OS release), a NUC, a VM with a USB audio device. The
  host OS's own Liquidsoap version doesn't matter.
- **Native** (`install.sh`): runs directly under systemd. Needs Raspberry Pi
  OS / Debian 13 "Trixie", whose Liquidsoap is new enough.

On a Pi 3B or newer, use **64-bit Raspberry Pi OS Lite** either way.

### Option A: Docker

**1. Install Docker** (once):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER      # log out and back in afterwards
```

**2. Mount the music share on the host.** Docker deliberately doesn't mount
it itself. If the SMB server were down at boot, a Docker-mounted share would
stop the engine container from starting at all, and there'd be no emergency
music. Mounted on the host, the engine always starts and plays from the
emergency folder until the share is back.

```bash
sudo apt install -y cifs-utils
sudo install -d /mnt/radio
printf 'username=YOUR_USER\npassword=YOUR_PASSWORD\n' | sudo tee /etc/radioplayer-smb >/dev/null
sudo chmod 600 /etc/radioplayer-smb
echo '//NAS/Music /mnt/radio cifs credentials=/etc/radioplayer-smb,ro,vers=3.0,iocharset=utf8,file_mode=0444,dir_mode=0555,soft,echo_interval=10,actimeo=30,nofail,_netdev,x-systemd.automount 0 0' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo systemctl restart remote-fs.target
ls /mnt/radio                       # should list your folders
```

Replace `//NAS/Music` with your server and share. `soft` makes reads fail
within seconds during an outage instead of hanging the player, and
`x-systemd.automount` remounts automatically when the server is back.

**3. Configure and start:**

```bash
git clone https://github.com/duffduff99/RadioPlayer.git
cd RadioPlayer
cp config/config.example.yaml config.yaml
nano config.yaml                    # mqtt section and output.device at minimum
aplay -L | grep -A1 ^plughw         # find your output device name
docker compose up -d --build        # first build on a Pi 3B takes several minutes
docker compose logs -f              # watch it start (Ctrl-C to stop watching)
```

Both containers (`engine` and `bridge`) restart automatically after a crash
or reboot. Day to day:

```bash
docker compose exec engine radioplayer status      # or any command in section 8
docker compose restart                             # after editing config.yaml
git pull && docker compose up -d --build           # update
```

Settings, the last playlist/level/preset and the emergency music copy live
in the Docker volume `radioplayer_state`, so they survive rebuilds.

Docker notes:
- The music folder is `/mnt/radio` by default. Use another host path with
  `MUSIC_DIR=/path docker compose up -d`.
- The share is passed in with `rslave` mount propagation, so the container
  sees it when the host remounts it after an outage. This needs the host's
  `/` to be a shared mount, which is the default on systemd systems
  (Raspberry Pi OS, Debian, Ubuntu). If `docker compose up` says *"not a
  shared or slave mount"*, run `sudo mount --make-rshared /`.
- The engine reaches the sound card through `/dev/snd` as group 29, the
  `audio` group on Debian, Raspberry Pi OS and Ubuntu. On other distros,
  put the output of `getent group audio` into `group_add` in
  `compose.yaml`.
- To build on a different base image: `docker compose build --build-arg BASE=ubuntu:questing`.
  The build refuses Liquidsoap older than 2.2.

### Option B: Native install

Needs Raspberry Pi OS / Debian 13 "Trixie" (Bookworm's Liquidsoap 2.1.3 is
too old, and the installer refuses it).

```bash
sudo apt install -y git
git clone https://github.com/duffduff99/RadioPlayer.git
cd RadioPlayer
sudo ./install.sh
```

The installer asks for the SMB server, share, user and password. It then:

- installs Liquidsoap, cifs-utils and a Python venv
- creates a `radioplayer` system user
- mounts the share read-only at `/mnt/radio` (automount, retries after outages)
- installs and starts `radioplayer-engine` and `radioplayer-bridge`

Then edit **`/etc/radioplayer/config.yaml`**. At minimum set the `mqtt`
section and `output.device`. Restart afterwards:

```bash
aplay -L | grep -A1 ^plughw          # find your output device name
sudo nano /etc/radioplayer/config.yaml
sudo -u radioplayer /opt/radioplayer/venv/bin/radioplayer check   # validate
sudo systemctl restart radioplayer-engine radioplayer-bridge
```

Re-running `./install.sh` after a `git pull` upgrades the code and keeps your
config.

### Both

On first start the bridge copies a random ~2 GB of the library to the SD
card as the emergency folder, and refreshes it weekly (`emergency_*` in
the config). With a library of a few hundred songs, raise
`emergency_max_mb` until the whole library fits.

---

## 3. Playlists

With `auto_playlists: true` (the default), every top-level folder on the share
is a playlist, and subfolders are included. Hidden folders and folders listed
in `exclude` are skipped. An **All Music** playlist covers the whole share.

To add a playlist, create a folder on the share and press **Rescan music
library** in Home Assistant. New *files* in existing folders are picked up
without any restart, within `reload_minutes` or immediately on Rescan. A
new or removed *folder* changes the playlist list, so Rescan restarts the
engine. **That's a few seconds of silence on air.**

Combine folders into one playlist in `config.yaml`:

```yaml
music:
  exclude: [Christmas, Winter]        # don't also show them on their own
  playlists:
    - name: Holiday Mix
      folders: [Christmas, Winter]
```

With several folders, each song comes from a randomly chosen folder, so each
folder gets about equal airtime regardless of how many songs it holds.

The last selected playlist, level and preset survive restarts and reboots.

---

## 4. Home Assistant

You need an MQTT broker (the **Mosquitto broker** add-on) and the MQTT
integration. Create an HA user for the bridge and put its credentials in the
`mqtt` section. The device **FM Radio Player** then appears automatically
with these entities:

| Entity | Type | |
|---|---|---|
| Playlist | select | choose playlist |
| Skip song | button | |
| Output level | number (dB slider) | `min_volume_db`…`max_volume_db` |
| Processing | select | flat / light / medium / heavy |
| Now playing | sensor | attributes: title, artist, album, filename, source |
| Rescan music library | button (config) | |
| Playing from | sensor (diagnostic) | playlist name, `all` (whole-library fallback), or `emergency` |
| Playing fallback | binary sensor (problem) | on if the requested playlist isn't playing for 20 s+ |

All entities go *unavailable* if either the bridge or the engine is down.
[docs/home-assistant.md](docs/home-assistant.md) has a dashboard card and
example automations (alert when on fallback, seasonal playlist switching).

---

## 5. Calibration: setting levels into the modulator (do this once)

The ZFMSM's maximum audio input is **2.0 Vp-p (0.707 V RMS for a sine)**.
DAC full scale varies a lot: many USB DACs put out 1–2 V RMS, i.e. up to
2.8× too much. The goal is to make the loudest signal the Pi can ever
produce land safely under 2.0 Vp-p, then lock that in.

The processing guarantees no sample exceeds `ceiling_db` (−1 dBFS). The
**calibration tone** is a 400 Hz sine at exactly that ceiling, i.e. the
loudest the program can ever get. **It goes out on air** while it's on.

1. **Fix the hardware mixer** so it can't drift: run `alsamixer`, press F6,
   choose your card, and set the main control to 0 dB (or 100 % if there's
   no dB scale). Then `sudo alsactl store`.
2. Connect the Pi to the modulator (the modulator is the load; measure with
   it connected).
3. Start the tone and set the level to 0 dB:
   ```bash
   R="docker compose exec engine radioplayer"   # Docker (run in the RadioPlayer folder)
   R=/opt/radioplayer/venv/bin/radioplayer      # native install
   $R volume 0
   $R tone on
   ```
4. Measure AC volts across one channel (tip to sleeve) with a multimeter on
   AC V. Lower the level with `$R volume -1`, `$R volume -2`, … until the
   meter reads **≤ 0.6 V RMS** (≈ 1.7 Vp-p, leaving a margin under 2.0 Vp-p).
   400 Hz is used because many meters only read AC accurately up to a few
   hundred Hz.
5. Note that dB value and stop the tone: `$R tone off`.
6. In `config.yaml`, set **`max_volume_db`** to that value (and
   `default_volume_db` to it or a bit lower). Restart. The Home Assistant
   slider can no longer go above it.
7. Set the modulator's **Aural Modulation Adj.** with normal program audio.
   Ideally use a deviation meter or an RTL-SDR (gqrx/SDR#) and aim for peaks
   around ±75 kHz. Without one: compare against a commercial station on the
   same receiver, and back off if you hear distortion on loud passages.

If you skip calibration, the defaults (`max_volume_db: 0`) give
−1 dBFS peaks. That's fine for the Pi's 3.5 mm jack, but probably too hot
for a 2 V RMS USB DAC.

---

## 6. Audio processing

Order of the chain, and why each stage is there:

1. **AGC**: slow gain riding (10 s up, 1 s down) toward `agc_target_db`, so
   quiet 1970s masters and loud modern masters come out similar. It holds
   its gain during silence and fades, so it won't boost hiss.
2. **High-pass at 30 Hz**: the modulator doesn't pass lower, and sub-bass
   wastes modulation.
3. **EQ**: bass (80 Hz), presence (3 kHz), air (10 kHz), per preset.
4. **4-band compressor** (`full`), with bands at 150 / 800 / 5000 Hz (Linkwitz-Riley
   crossovers, flat ±0.6 dB when bypassed). In `lite`, one wide-band
   compressor instead.
5. **Limiter** (`limiter_drive` pushes into it, which is what makes "heavy"
   louder), then a **clip** at ceiling −1 dB.
6. **8th-order low-pass at 15 kHz**, after the clip so clip distortion is
   filtered too.
7. **Pre-emphasis protection** (`preemphasis_us`, see below): the audio is
   treble-boosted exactly as the transmitter will boost it, limited and
   clipped there, then the boost is undone, followed by a second 15 kHz
   low-pass. Overall: −5.5 dB at 15 kHz, **−100 dB at 19 kHz**.
8. **Safety clip** at `ceiling_db`. Nothing louder reaches the DAC.
9. **Output level** (the Home Assistant slider), ≤ `max_volume_db`.

Controls react within about a second: the engine keeps two 0.5 s buffers
inside the chain, which cuts its CPU use by about 5×.

Measured output on synthetic music-like test material (RMS; higher is louder):

| Preset | full | lite |
|---|---|---|
| flat | −18.5 dBFS | −18.5 |
| light | −17.9 | −17.5 |
| medium | −16.4 | −15.8 |
| heavy | −15.1 | −14.5 |

In every test the peaks stayed ≤ −1.00 dBFS (with pre-emphasis protection
on, music peaks land around −2 dBFS).

Presets live in `config.yaml` under `processing.presets`. Override any value
or add your own. This is lighter-touch than a commercial station's processor
(heavy is around 13 dB crest factor, where commercial FM often runs 8–10),
on purpose: it's tuned not to pump or distort.

### Pre-emphasis (why bright songs clip on FM)

Every FM radio applies **de-emphasis**, a treble cut (75 µs in the Americas:
−10 dB at 5 kHz, −17 dB at 15 kHz). To compensate, the transmitter boosts
treble by the same amount before modulating (**pre-emphasis**). The ZFMSM spec
sheet doesn't mention it, but you can tell by ear: if music through it sounds
normally bright on an ordinary radio, the modulator is pre-emphasizing.
Without pre-emphasis, everything would sound clearly muffled.

That boost is why a player that only watches its own output level (VolumIO,
for example) can still over-deviate the transmitter. Its −1 dBFS peaks become
up to +17 dB on cymbals and "s" sounds by the time they reach the modulator's
FM stage. Measured on test material with protection off, peaks after a
simulated 75 µs pre-emphasis reached **+5 to +16.5 dB over full scale**.

With `preemphasis_us: 75` (the default), the processor applies the same
boost internally, limits and clips the boosted signal just under the
ceiling, then exactly undoes the boost. At low levels this is transparent
(±0.6 dB to 14 kHz). On loud, bright material, after simulated pre-emphasis
(the calibration tone, i.e. 100 % modulation, sits at −1 dBFS):

| Test material | 99.9 % of samples, off | 99.9 %, protected |
|---|---|---|
| hot pink noise | +3.0 dBFS | +0.6 dBFS |
| music-like (bright hi-hats) | +6.8 dBFS | +0.5 dBFS |
| loud white noise (extreme) | +4.2 dBFS | +1.3 dBFS |

So treble overs drop from 4–8 dB above 100 % to about 1.5–2 dB, briefly. I
could squeeze that to 0 dB, but only by limiting bass too, which costs about
1.5 dB of loudness and audibly clips kick drums. I chose the middle ground.
The real modulator's curve is only approximately known anyway. The cost is
slightly reined-in treble on the brightest moments, as on any commercial FM
station. If your modulator turns out not to pre-emphasize, set
`preemphasis_us: 0`. If you want broadcast-grade processing beyond this,
Liquidsoap 2.3 can host [Stereo Tool](https://www.stereotool.com/) (free for
non-commercial use).

---

## 7. How "never silent" works

| Failure | What happens |
|---|---|
| Selected playlist empty or unreadable | Plays the whole library (source `all`); "Playing fallback" turns on |
| SMB server down or network lost | Soft mount fails reads within seconds instead of hanging; plays the **emergency folder** (local SD card) and returns automatically when the share is back |
| Share down at boot | Automount retries on access; playlist list comes from cache |
| Emergency folder also empty | Silence (the last resort). The bridge fills it on first start; check the bridge log if it's empty |
| Processing produces NaN/garbage, or output dead while input has audio | Watchdog restarts the engine within ~10 s |
| Engine or bridge crash | systemd or Docker restarts it (no retry limit) |
| Broken config.yaml | Engine keeps running on the last good settings |
| Song with long silence (hidden tracks) | Skipped after 10 s of silence |

---

## 8. Command line

```bash
R="docker compose exec engine radioplayer"   # Docker
R=/opt/radioplayer/venv/bin/radioplayer      # native
$R status                 # what's playing (JSON)
$R playlist "Holiday Mix"
$R skip
$R volume -6              # dB, clamped to min/max_volume_db
$R preset heavy
$R reload                 # re-read all folders now
$R tone on|off            # calibration tone (ON AIR)
$R check                  # validate config, list playlists
$R refresh-emergency --max-mb 2000    # native: prefix with sudo -u radioplayer
docker compose logs -f                                 # Docker logs
journalctl -u radioplayer-engine -u radioplayer-bridge -f   # native logs
```

## 9. Troubleshooting

Logs: `docker compose logs engine` / `docker compose logs bridge` (Docker),
or `journalctl -u radioplayer-engine` / `-u radioplayer-bridge` (native).

- **No audio, engine running:** check `output.device` against `aplay -L`
  (run on the host), and `alsamixer` levels. Check the engine log.
- **"Playing fallback" stays on:** `radioplayer status` shows `source`.
  `emergency` means the share is unreachable (`ls /mnt/radio` on the host);
  `all` means the selected folder has no playable files.
- **HA entities unavailable:** check the bridge log (broker address or
  credentials) and whether the engine is running.
- **Dropouts/stutter:** CPU (see section 1). Set `processing.mode: lite`.
- **Changing SMB server or credentials:** Docker: edit the line in
  `/etc/fstab` and `/etc/radioplayer-smb`, then
  `sudo systemctl daemon-reload && sudo systemctl restart remote-fs.target`.
  Native: edit `/etc/systemd/system/mnt-radio.mount` and
  `/etc/radioplayer/smb-credentials`, then
  `sudo systemctl daemon-reload && sudo systemctl restart mnt-radio.automount`.

## 10. What was and wasn't tested

Tested in a Linux container, on Liquidsoap **2.2.4 and 2.3.2**:
playlist switching, skip, level clamping, presets, state persistence,
fallback to the emergency folder and recovery when the share disappears and
returns, the watchdog, frequency response, peak ceiling, CPU use, MQTT
discovery and commands against a local Mosquitto broker, and ALSA output
(via ALSA's null device). `pytest` covers the config, generator and bridge,
and boots the real engine.

Pre-emphasis protection was measured by applying an analog-model 75 µs
pre-emphasis to the engine's output.

The Docker setup was built and run with `compose.yaml` (on an Ubuntu 25.10
base, which has the same Liquidsoap 2.3.2 as Trixie; Debian's mirrors weren't
reachable from the test environment). Tested: both containers starting,
health check, MQTT discovery and commands, the CLI via `docker compose
exec`, Rescan restarting the engine with settings kept, the emergency folder
filling on first start, and a host mount made after start appearing inside
the container.

**Not tested:** a real Raspberry Pi (CPU numbers are estimates), a real SMB
server (outages were simulated by removing the folder; a hung server is
handled by the `soft` mount option, which I couldn't exercise), a real sound
card, and a real Home Assistant instance (the discovery payloads follow the
documented MQTT format).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest            # engine tests run if liquidsoap + ffmpeg are installed
```

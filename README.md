# RadioPlayer

Always-on music playout for a small FM station: it feeds a line-level FM
modulator (built for a Blonder Tongue ZFMSM) from a USB sound card. It plays
shuffled folders from a network share, processes the audio for FM, and is
controlled from Home Assistant.

It runs as two Docker containers on any Linux machine: a server or VM
(recommended), a mini PC, or a Raspberry Pi 4/5. A Raspberry Pi 3B works
with reduced processing.

- **Playlists are folders.** Every top-level folder of the music library
  becomes a playlist automatically. You can also define playlists that
  combine folders. Everything shuffles, and no song repeats until the whole
  folder has played. FLAC, MP3, Ogg, Opus, M4A/AAC and WAV all play as they
  are; nothing needs converting.
- **Home Assistant control** over MQTT (auto-discovered): playlist, skip,
  output level, processing preset, now playing, and a "playing fallback" alarm.
- **Never silent.** The library can be mirrored to local disk, so a NAS or
  network outage has no effect on air. The selected playlist falls back to
  the whole library. A watchdog restarts the engine if the output ever goes
  dead while music is coming in, and Docker restarts anything that crashes.
- **FM processing:** slow AGC, EQ, 4-band compressor, limiter, 15 kHz
  low-pass (protects the 19 kHz stereo pilot), pre-emphasis protection so
  bright songs don't over-deviate the transmitter, and a hard ceiling no
  sample can exceed.
- **Output level control** that cannot overdrive the modulator. The level
  is applied after the limiter and capped at a maximum you set.

```
 music share ──► local mirror ──► Liquidsoap engine ──────────────► USB sound card ──► modulator L/R
 (SMB, on host)   (optional)       playlists → fallback → AGC → EQ → multiband
                                   → limiter → 15 kHz LPF → ceiling clip → level
                                        ▲ HTTP (private network)
                                        │
                                  radioplayer bridge ◄──── MQTT ────► Home Assistant
```

---

## 1. Hardware

### The computer

The audio engine (Liquidsoap) does its processing on a single CPU core, so
single-core speed is what matters, not core count. It uses about 170 MB of
RAM.

| Machine | `processing.mode` | Load on one core |
|---|---|---|
| PC, server or VM (measured on a 2.1 GHz Xeon) | `full` | 8–9% |
| Raspberry Pi 4 / 5 | `full` | not measured; roughly 2–3× faster per core than a Pi 3B |
| Raspberry Pi 3B (measured) | `lite` | 50–70% |
| Raspberry Pi 3B (measured) | `full` | ~105%: too slow, audio stutters |

The modes:

| `processing.mode` | What runs |
|---|---|
| `full` | AGC, EQ, 4-band compressor, limiter, protection |
| `lite` | AGC, EQ, 1-band compressor, limiter, protection |
| `minimal` | limiter, protection |

"Protection" is everything that keeps the transmitter safe: the 15 kHz
low-pass, pre-emphasis protection and the hard ceiling. It is in every mode.
If the engine log shows `Underrun!` or `catchup`, or the audio stutters,
step down a mode. Check the load with `docker stats --no-stream`.

**Virtual machines (Proxmox and others):** pass the USB sound card through
to the VM (in Proxmox: Hardware → Add → USB Device, by vendor/device ID).
Two CPU cores for the VM is plenty.

### Audio output

Any class-compliant USB sound card or DAC works, including inexpensive
USB headphone adapters. Keep the analog cable to the modulator short.

A Raspberry Pi's built-in 3.5 mm jack also works, but it has an audible
noise floor; a USB sound card is better.

### Connecting to the ZFMSM

From the spec sheet: **audio inputs are F-connectors (L and R), max input
2.0 Vp-p, audio range 30 Hz – 15 kHz.** You need a 3.5 mm stereo → 2× RCA
cable plus two RCA-female → F-male adapters.

If you hear hum, it's usually a ground loop between the computer and the
headend. Fix it with a stereo ground-loop isolator on the audio cable.

---

## 2. Install

The steps are the same on a server, a VM or a Raspberry Pi. On a Pi, use
64-bit Raspberry Pi OS Lite.

**1. Install Docker** (once):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER      # log out and back in afterwards
```

**2. Check the sound card** and note its card name:

```bash
sudo apt install -y alsa-utils cifs-utils
aplay -l                            # e.g. "card 1: Audio [AB13X USB Audio]"
```

The name after `card N:` (here `Audio`) goes into the config as
`plughw:CARD=Audio,DEV=0`. It varies between sound cards.

**3. Mount the music share on the host.** Docker deliberately doesn't mount
it itself: if the SMB server were down at boot, a Docker-mounted share would
stop the engine container from starting at all.

```bash
sudo install -d /mnt/radio
printf 'username=YOUR_USER\npassword=YOUR_PASSWORD\n' | sudo tee /etc/radioplayer-smb >/dev/null
sudo chmod 600 /etc/radioplayer-smb
echo '//NAS/Music /mnt/radio cifs credentials=/etc/radioplayer-smb,ro,vers=3.0,iocharset=utf8,file_mode=0444,dir_mode=0555,soft,echo_interval=10,actimeo=30,nofail,_netdev,x-systemd.automount 0 0' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo systemctl restart remote-fs.target
ls /mnt/radio                       # should list your folders
```

Replace `//NAS/Music` with your server and share. `soft` makes reads fail
within seconds during an outage instead of hanging, and
`x-systemd.automount` remounts automatically when the server is back.

**4. Start it,** either from a stack manager or from the command line. A
basic setup needs only a few environment variables:

| Variable | Meaning | Example |
|---|---|---|
| `MQTT_HOST` | Home Assistant's MQTT broker (name or IP) | `mqtt.example.lan` |
| `MQTT_USERNAME`, `MQTT_PASSWORD` | broker login | |
| `MQTT_PORT` | broker port, if not 1883 | |
| `AUDIO_DEVICE` | the sound card from step 2 | `plughw:CARD=Audio,DEV=0` |
| `MUSIC_ROOT` | music folder as the container sees it; the share is at `/mnt/radio` | `/mnt/radio/Radio` |
| `MIRROR` | `true` to play from a local copy (see below) | `true` |
| `PROCESSING_MODE` | `full`, `lite` or `minimal` | `full` |

*Stack manager (Dockhand, Portainer, ...):* create a stack from this Git
repository with compose file `compose.yaml`, and enter the variables above
as the stack's environment.

*Command line:*

```bash
git clone https://github.com/duffduff99/RadioPlayer.git
cd RadioPlayer
nano .env                           # the variables above, one NAME=value per line
docker compose up -d                # pulls the published image
docker compose logs -f              # watch it start (Ctrl-C to stop watching)
```

Either way you get two containers, `engine` and `bridge`, which restart
automatically after a crash or reboot. They talk to each other on the
stack's own private network; no ports are opened on the host. The image is
`ghcr.io/duffduff99/radioplayer:latest`, built by GitHub from `main` for
PCs/servers (amd64) and Raspberry Pi (arm64).

**5. Fine-tune in `config.yaml` (optional).** On first start a default
`config.yaml`, with every setting explained, is created on the `config`
volume. Everything beyond the variables above is set there: combined
playlists, output level limits, processing presets, crossfade and so on. See
[config/config.example.yaml](config/config.example.yaml) for the contents.

- The file is on the Docker host at
  `/var/lib/docker/volumes/<stack>_config/_data/config.yaml` (edit it as
  root), or use your stack manager's volume browser.
- To keep it in a folder of your choice instead, set `CONFIG_DIR` to a host
  folder, e.g. `CONFIG_DIR=/opt/radioplayer`. If the folder has no
  `config.yaml` yet, create one or make the folder writable for the
  container.
- An environment variable always wins over the same setting in the file.
  Any setting can be given as a variable to the containers in the form
  `RADIOPLAYER__SECTION__KEY`, e.g. `RADIOPLAYER__OUTPUT__CROSSFADE_SECONDS=0`.
- Restart the stack after changing the file or the variables.

Day to day:

```bash
docker compose exec engine radioplayer status      # or any command in section 9
docker compose restart                             # after editing config.yaml
docker compose pull && docker compose up -d        # update
```

The last playlist/level/preset and the local music copy live in the Docker
volume `state`, and the configuration in the volume `config`, so both
survive updates.

### The local mirror

With `music.mirror.enabled: true`, the whole library is copied to local disk
and played from there, so a NAS or network outage has no effect on air. Use
it whenever the library fits on the local disk.

- The first sync runs right after the first start; music begins when it
  finishes.
- The share is re-checked every 15 minutes (`interval_minutes`) and when
  **Rescan music library** is pressed in Home Assistant. New and changed
  files are copied; files removed from the share are removed.
- It never deletes because the share is unreachable or looks mostly empty.
  To confirm a real mass deletion, run `radioplayer sync --force`.

Without the mirror, the engine reads the share directly and the bridge keeps
a random ~2 GB of it on local disk as an emergency folder (`emergency_*` in
the config), played while the share is unreachable.

### Notes

- **Other variables** for `compose.yaml`: `MUSIC_DIR` (where the share is
  mounted on the host, default `/mnt/radio`) and `RADIOPLAYER_IMAGE`.
- **MQTT broker address:** the containers use Docker's regular networking,
  so `MQTT_HOST` must be a DNS name or an IP address. mDNS names such as
  `homeassistant.local` usually don't resolve there.
- **Build it yourself** instead of pulling: `docker compose up -d --build`
  (several minutes on a Pi 3B). The build refuses Liquidsoap older than 2.2.
- **"not a shared or slave mount" on start:** the share is passed in with
  `rslave` propagation so the container sees it when the host remounts it.
  That needs the host's `/` to be a shared mount, the default on systemd
  systems. If Docker reports this error, run `sudo mount --make-rshared /`.
- **Sound card access:** the engine reaches `/dev/snd` as group 29, the
  `audio` group on Debian, Ubuntu and Raspberry Pi OS. On other
  distributions, put the number from `getent group audio` into `group_add`
  in `compose.yaml`. If the card is unplugged or re-attached, the engine
  restarts until it can open it again.
- **Latency profile:** `engine.low_cpu` (default `auto`) picks small audio
  buffers on PCs and servers, so a skip is heard after about a second, and
  large ones on ARM boards, where it takes 2–4 seconds but costs far less
  CPU.

### Without Docker

`sudo ./install.sh` installs the same thing as systemd services on Debian 13
"Trixie" (Liquidsoap 2.2 or newer is required; Debian 12's is too old). It
asks for the share details and mounts the share itself; the config is then
`/etc/radioplayer/config.yaml`.

It does **not** work on Raspberry Pi OS: Debian's Liquidsoap package crashes
on start with Raspberry Pi OS's own FFmpeg build, and the installer stops
with an explanation. Use Docker there.

---

## 3. Playlists

With `auto_playlists: true` (the default), every top-level folder under
`music.root` is a playlist, and subfolders are included. Hidden folders and
folders listed in `exclude` are skipped. An **All Music** playlist covers
the whole library.

To add music, copy files into a folder on the share. To add a playlist,
create a folder. Changes are picked up at the next mirror sync (every 15
minutes), or immediately when you press **Rescan music library** in Home
Assistant. New *files* never interrupt playback. A new or removed *folder*
changes the playlist list, which restarts the engine: **a few seconds of
silence on air.** Without the mirror, new files are found within
`reload_minutes` and folder changes need Rescan.

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
2.8× too much, while small USB headphone adapters may stay under the limit
even at full level. The goal is to make the loudest signal the player can
ever produce land safely under 2.0 Vp-p, then lock that in.

The processing guarantees no sample exceeds `ceiling_db` (−1 dBFS). The
**calibration tone** is a 400 Hz sine at exactly that ceiling, i.e. the
loudest the program can ever get. **It goes out on air** while it's on.

1. **Fix the hardware mixer** so it can't drift: on the host run `alsamixer`, press F6,
   choose your card, and set the main control to 0 dB (or 100 % if there's
   no dB scale). Then `sudo alsactl store`.
2. Connect the sound card to the modulator (the modulator is the load;
   measure with it connected).
3. Start the tone and set the level to 0 dB:
   ```bash
   R="docker compose exec engine radioplayer"   # or the engine container's console
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
   slider can no longer go above it. If the meter already reads 0.6 V or
   less at `volume 0`, leave `max_volume_db: 0`.
7. Set the modulator's **Aural Modulation Adj.** with normal program audio.
   Ideally use a deviation meter or an RTL-SDR (gqrx/SDR#) and aim for peaks
   around ±75 kHz. Without one: compare against a commercial station on the
   same receiver, and back off if you hear distortion on loud passages.

If you skip calibration, the defaults (`max_volume_db: 0`) give −1 dBFS
peaks, which is too hot for a sound card with a 2 V RMS output.

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

So treble overs drop from 4–8 dB above 100 % to about 1.5–2 dB, briefly.
Squeezing that to 0 dB would mean limiting bass too, which costs about
1.5 dB of loudness and audibly clips kick drums, so the chain takes the
middle ground. The cost is slightly reined-in treble on the brightest
moments, as on any commercial FM station. If your modulator turns out not to
pre-emphasize, set `preemphasis_us: 0`.

For broadcast-grade processing beyond this, Liquidsoap can host
[Stereo Tool](https://www.stereotool.com/). This image does not support it
yet: Debian's Liquidsoap is built without the Stereo Tool plugin.

---

## 7. How "never silent" works

| Failure | What happens |
|---|---|
| Selected playlist empty or unreadable | Plays the whole library (source `all`); "Playing fallback" turns on |
| SMB server down or network lost, **mirror on** | Nothing: the engine plays the local copy; the mirror updates when the share is back |
| SMB server down or network lost, mirror off | Soft mount fails reads within seconds instead of hanging; plays the **emergency folder** (local disk) and returns automatically when the share is back |
| Share down at boot | Automount retries on access; with the mirror on, the local copy plays regardless |
| No local music at all (first start, before the first sync or emergency copy finishes) | Silence, the last resort. Check the bridge log |
| USB sound card unplugged or re-attached | Engine exits and is restarted until it can open the card again |
| Processing produces NaN/garbage, or output dead while input has audio | Watchdog restarts the engine within ~10 s |
| Engine or bridge crash | Docker (or systemd) restarts it, with no retry limit |
| Broken config.yaml | Engine keeps running on the last good settings |
| Song with long silence (hidden tracks) | Skipped after 10 s of silence |

---

## 8. Measuring the transmitter chain

To set the EQ from measurements instead of by ear, play a known signal and
record what a receiver gets (ideally an SDR on the coax, with **de-emphasis
switched off** in the SDR software, saved as WAV or FLAC).

- Through the player: `radioplayer signal sweep` plays stepped tones from
  30 Hz to 15 kHz (2 s each, looping, 1 kHz first) at 20 dB below the
  ceiling, after all processing. Stop with `radioplayer signal off`.
- Straight into the modulator (to measure the modulator alone):
  `python3 tools/make_test_signal.py test.wav` writes the same tones as a
  file to play from any device.

Comparing the level of each tone in the recording with what was sent gives
the frequency response, and shows whether the modulator applies
pre-emphasis.

## 9. Command line

```bash
R="docker compose exec engine radioplayer"   # or use the engine container's console
$R status                 # what's playing (JSON)
$R playlist "Holiday Mix"
$R skip
$R volume -6              # dB, clamped to min/max_volume_db
$R preset heavy
$R reload                 # re-read all folders now
$R tone on|off            # calibration tone (ON AIR)
$R signal sweep|off       # measurement tones, 20 dB below the ceiling (ON AIR)
$R sync                   # update the local mirror now (if enabled)
$R check                  # validate config, list playlists
$R refresh-emergency --max-mb 2000    # refill the emergency folder (mirror off)
docker compose logs -f                # logs
```

With the systemd install, the command is
`/opt/radioplayer/venv/bin/radioplayer` and the logs are in
`journalctl -u radioplayer-engine -u radioplayer-bridge`.

## 10. Troubleshooting

Logs: `docker compose logs engine` and `docker compose logs bridge`, or the
container logs in your stack manager.

- **Engine keeps restarting with an ALSA error:** "No such device" means
  `output.device` doesn't match the card; run `aplay -l` on the host and use
  the card name shown there. "Device or resource busy" means something else
  holds the card; `sudo fuser -v /dev/snd/*` on the host lists it. (On the
  test Pi this error also appeared during the very first start and cleared
  by itself; the cause was not found.)
- **No audio, engine running:** check `alsamixer` levels on the host and the
  engine log.
- **Choppy audio, `Underrun!` or `catchup` in the engine log:** the CPU
  can't keep up. Step `processing.mode` down (`full` → `lite` → `minimal`).
  On a Raspberry Pi also run `vcgencmd get_throttled`: anything other than
  `0x0` means an inadequate power supply or overheating is slowing it down.
- **Radio missing from Home Assistant:** the bridge announces it only once
  both the MQTT broker and the engine are reachable. The bridge log shows
  `Connected to MQTT broker`, then `Engine online` and `Published Home
  Assistant discovery`. `MQTT_HOST` must be a DNS name or IP address, not a
  `.local` name.
- **"Playing fallback" stays on:** `radioplayer status` shows `source`.
  `all` means the selected folder has no playable files; `emergency` means
  the share is unreachable (`ls /mnt/radio` on the host).
- **Image pull refused:** the package on ghcr.io is private. Make it public
  in the repository's Packages settings on GitHub, or run
  `docker login ghcr.io` on the host.
- **Changing SMB server or credentials:** edit the line in `/etc/fstab` and
  `/etc/radioplayer-smb`, then
  `sudo systemctl daemon-reload && sudo systemctl restart remote-fs.target`.

## 11. What was and wasn't tested

**On real hardware:** a Raspberry Pi 3B running the Docker setup with a USB
sound card into the ZFMSM, controlled from Home Assistant. `lite` mode plays
steadily there, and the cymbal and "s" distortion heard with an unprocessed
player is gone.

**In containers,** on Liquidsoap 2.2.4 and 2.3.2: playlist switching, skip,
level clamping, presets, state persistence, the watchdog, frequency
response, peak ceiling, CPU use, a 5-minute soak without dropouts, MQTT
discovery and commands against a Mosquitto broker, the mirror (first sync,
added and removed folders, a simulated share outage), fallback to the
emergency folder and recovery, FLAC playback including 96 kHz/24-bit, and
the measurement sweep. Pre-emphasis protection was measured by applying an
analog-model 75 µs pre-emphasis to the engine's output. `pytest` covers the
config, generator, mirror and bridge, and boots the real engine.

**Not tested:** a hung SMB server (outages were simulated by removing the
folder; the `soft` mount option is what handles a hang), a USB sound card
being re-plugged, the systemd install on real hardware, and Raspberry Pi 4/5
performance.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest            # engine tests run if liquidsoap + ffmpeg are installed
```

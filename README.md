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
  low-pass (protects the 19 kHz stereo pilot), and a hard ceiling no sample
  can exceed.
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

The audio engine (Liquidsoap) runs its processing on a single CPU core.
Measured on a 2.1 GHz Xeon core with the settings shipped here:

| Processing mode | Liquidsoap 2.3.2 | Liquidsoap 2.2.4 |
|---|---|---|
| `full` (4-band) | 12.8 % of one core | 13.4 % |
| `lite` (1-band) | 6.5 % | ~8 % |

I could not test on Pi hardware. As a rough guide, a Pi core is around 2.5–3×
slower than that Xeon on a Pi 4, around 5× on a Pi 3, and around 7–10× on a
Pi 2. That suggests:

| Board | Expectation |
|---|---|
| **Pi 4 (2 GB+) — recommended** | `full` comfortably (~35–40 % of a core) |
| Pi 5 | `full`, lots of headroom |
| Pi 3B/3B+ | `full` probably works but is tight; `lite` is safe |
| Pi 2 | Use `lite`. `full` probably cannot keep up. Even `lite` is unproven. |

Check on your board: `top -p $(pgrep liquidsoap)`. Sustained use near 100 %
of a core means audio dropouts; switch `processing.mode` to `lite`.

Other reasons to move off the Pi 2: 64-bit Raspberry Pi OS needs a Pi 3 or
newer (or the rare Pi 2 v1.2), and the Pi 4 has much faster Ethernet for the
SMB share. The Pi 2's 1 GB of RAM is fine; the engine uses about 170 MB.

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

Use **Raspberry Pi OS Lite based on Debian 13 "Trixie"** (64-bit on a
Pi 3/4/5). It ships Liquidsoap 2.3.2. Bookworm ships Liquidsoap 2.1.3, which
is too old, and the installer refuses it.

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
- copies about 2 GB of random songs to the SD card as the emergency folder,
  refreshed weekly

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
   R=/opt/radioplayer/venv/bin/radioplayer
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
   filtered too. Measured: −3 dB at 15 kHz, **−62 dB at 19 kHz**.
7. **Safety clip** at `ceiling_db`. Nothing louder reaches the DAC.
8. **Output level** (the Home Assistant slider), ≤ `max_volume_db`.

Measured output on synthetic music-like test material (RMS; higher is louder):

| Preset | full | lite |
|---|---|---|
| flat | −17.6 dBFS | −17.6 |
| light | −17.1 | −16.6 |
| medium | −15.5 | −15.1 |
| heavy | −14.2 | −13.6 |

In every test the peaks stayed ≤ −1.00 dBFS.

Presets live in `config.yaml` under `processing.presets`. Override any value
or add your own. This is lighter-touch than a commercial station's processor
(heavy is around 13 dB crest factor, where commercial FM often runs 8–10),
on purpose: it's tuned not to pump or distort.

**Pre-emphasis, an honest caveat.** US FM uses 75 µs pre-emphasis (+17 dB
at 15 kHz). The ZFMSM spec sheet doesn't say whether it applies it
internally; most CATV modulators do. If it does, bright material can
over-deviate even though the input level is controlled. The high band is
therefore compressed harder than the others. That is an approximation, not
a true pre-emphasis-aware limiter like a commercial processor's. If you
need more, Liquidsoap can host [Stereo Tool](https://www.stereotool.com/)
(free for non-commercial use, has Pi builds), which does this properly.

---

## 7. How "never silent" works

| Failure | What happens |
|---|---|
| Selected playlist empty or unreadable | Plays the whole library (source `all`); "Playing fallback" turns on |
| SMB server down or network lost | Soft mount fails reads within seconds instead of hanging; plays the **emergency folder** (local SD card) and returns automatically when the share is back |
| Share down at boot | Automount retries on access; playlist list comes from cache |
| Emergency folder also empty | Silence (the last resort); fill it with `radioplayer refresh-emergency` |
| Processing produces NaN/garbage, or output dead while input has audio | Watchdog restarts the engine within ~10 s |
| Engine or bridge crash | systemd restarts it (no retry limit) |
| Broken config.yaml | Engine keeps running on the last good settings |
| Song with long silence (hidden tracks) | Skipped after 10 s of silence |

---

## 8. Command line

```bash
R=/opt/radioplayer/venv/bin/radioplayer
$R status                 # what's playing (JSON)
$R playlist "Holiday Mix"
$R skip
$R volume -6              # dB, clamped to min/max_volume_db
$R preset heavy
$R reload                 # re-read all folders now
$R tone on|off            # calibration tone (ON AIR)
$R check                  # validate config, list playlists
sudo -u radioplayer $R refresh-emergency --max-mb 2000
journalctl -u radioplayer-engine -u radioplayer-bridge -f
```

## 9. Troubleshooting

- **No audio, engine running:** check `output.device` against
  `aplay -L`, and `alsamixer` levels. Run
  `journalctl -u radioplayer-engine -n 50`.
- **"Playing fallback" stays on:** `radioplayer status` shows `source`.
  `emergency` means the share is unreachable (`systemctl status mnt-radio.mount`,
  `ls /mnt/radio`); `all` means the selected folder has no playable files.
- **HA entities unavailable:** check `journalctl -u radioplayer-bridge`
  (broker address or credentials) and whether the engine is running.
- **Dropouts/stutter:** CPU (see section 1). Set `processing.mode: lite`.
- **Changing SMB server or credentials:** edit
  `/etc/systemd/system/mnt-radio.mount` and `/etc/radioplayer/smb-credentials`,
  then `sudo systemctl daemon-reload && sudo systemctl restart mnt-radio.automount`.

## 10. What was and wasn't tested

Tested in a Linux container, on Liquidsoap **2.2.4 and 2.3.2**:
playlist switching, skip, level clamping, presets, state persistence,
fallback to the emergency folder and recovery when the share disappears and
returns, the watchdog, frequency response, peak ceiling, CPU use, MQTT
discovery and commands against a local Mosquitto broker, and ALSA output
(via ALSA's null device). `pytest` covers the config, generator and bridge,
and boots the real engine.

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

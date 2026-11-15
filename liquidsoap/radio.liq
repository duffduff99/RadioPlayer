#!/usr/bin/liquidsoap
# RadioPlayer audio engine.
#
# Plays shuffled folders from an SMB share, runs an FM-style processing chain
# (AGC -> EQ -> multiband compression -> limiter -> 15 kHz low-pass -> safety
# clip -> output trim) and never goes silent: if the selected playlist is
# unavailable it falls back to the whole library, then to a local emergency
# folder on the SD card.
#
# Do not edit settings here. They come from /etc/radioplayer/config.yaml via
# `radioplayer generate`, which writes the file included below.
#
# Tested against Liquidsoap 2.2.x and 2.3.x.

%include "/var/lib/radioplayer/generated.liq"

settings.log.stdout := true
settings.log.file := false
settings.log.level := 3
settings.init.allow_root := true
settings.frame.audio.samplerate := 44100
# Larger frames cut per-frame overhead: on Liquidsoap 2.3 this takes CPU use
# from ~4x to ~1x of what 2.2 needs. Controls react within ~0.2 s, which
# does not matter for a radio station.
settings.frame.duration := 0.2
settings.harbor.bind_addrs := [rp_http_bind]

log.important(label="radioplayer", "Starting RadioPlayer engine")

# --------------------------------------------------------------------------
# Runtime state (changed over HTTP, persisted to rp_state_file)
# --------------------------------------------------------------------------

current_playlist = ref(rp_initial_playlist)
volume_db = ref(rp_initial_volume_db)
current_preset = ref(rp_initial_preset)
tone_on = ref(false)

def clamp(lo, hi, x) =
  if x < lo then lo elsif x > hi then hi else x end
end

def save_state() =
  data = json.stringify(compact=true, {
    playlist = current_playlist(),
    volume_db = volume_db(),
    preset = current_preset()
  })
  try
    file.write(data=data, rp_state_file)
  catch err do
    log.severe(label="radioplayer", "Could not write state file: #{err}")
  end
end

# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

# Tag every track with the name of the source it came from, so status can
# report whether we are on the requested playlist or a fallback.
def tag(name, s) =
  metadata.map(fun (_) -> [("rp_source", name)], s)
end

# One shuffled folder. "randomize" plays every file once in random order
# before reshuffling, so songs do not repeat until the folder is exhausted.
# Tracks with long silence (hidden tracks, long fade tails) are skipped.
reloaders = ref([])

def folder(id, path) =
  s = playlist(
    id=id,
    mode="randomize",
    reload_mode="seconds",
    reload=rp_reload_seconds,
    path
  )
  reloaders := list.append(reloaders(), [fun () -> s.reload()])
  blank.skip(threshold=-45., max_blank=rp_max_blank, s)
end

# A playlist made of one or more folders. Multiple folders are picked
# between at random on each track (equal chance per folder).
def make_playlist(index, name, folders) =
  sources = list.mapi(fun (j, f) -> folder("pl_#{index}_#{j}", f), folders)
  s = if list.length(sources) == 1 then list.hd(sources) else random(id="pl_#{index}", sources) end
  tag(name, s)
end

playlist_sources = list.mapi(fun (i, p) -> (fst(p), make_playlist(i, fst(p), snd(p))), rp_playlists)

def playlist_exists(name) =
  list.assoc.mem(name, playlist_sources)
end

if not playlist_exists(current_playlist()) then
  current_playlist := fst(list.hd(playlist_sources))
end

# Selection changes take effect immediately (not at the end of the song).
# The change starts a new track, so the crossfade below smooths it.
selected = switch(
  id="selected",
  track_sensitive=false,
  list.map(fun (p) -> ({current_playlist() == fst(p)}, snd(p)), playlist_sources)
)

chain = [selected]
chain = if rp_fallback_to_all then list.append(chain, [tag("all", folder("all", rp_music_root))]) else chain end
chain = list.append(chain, [tag("emergency", folder("emergency", rp_emergency_dir))])

music = fallback(id="music", track_sensitive=false, chain)
music_fallback = music

music = if rp_crossfade > 0. then
  crossfade(duration=rp_crossfade, fade_in=rp_crossfade, fade_out=rp_crossfade, music)
else
  music
end

# Last resort so the output never stops. If we get here both the SMB share
# and the local emergency folder failed.
music = mksafe(music)

# Cut the source graph here and after the multiband compressor. Every frame
# Liquidsoap checks readiness by walking the whole graph, and the multiband
# split makes each walk revisit everything upstream once per branch; without
# these buffers the full chain costs ~5x more CPU (measured: 72% -> 14% of a
# core). Each adds 0.5 s of latency, which only delays controls slightly.
# max caps the backlog: the default (10 s) lets a buffer fill up after the
# engine has fallen behind, which then delays every skip by that much.
def graph_cut(s) =
  mksafe(buffer(buffer=0.5, max=1.5, s))
end
music = graph_cut(music)

# --------------------------------------------------------------------------
# Processing chain (parameters are refs so presets can change them live)
# --------------------------------------------------------------------------

p_drive = ref(0.)
p_bass = ref(0.)
p_presence = ref(0.)
p_air = ref(0.)
p_limiter_drive = ref(0.)
wide = {threshold = ref(0.), ratio = ref(1.), gain = ref(0.)}

# Band crossovers (Hz). Four bands: < c1, c1-c2, c2-c3, > c3.
band_refs = list.map(fun (_) -> {threshold = ref(0.), ratio = ref(1.), gain = ref(0.)}, [1, 2, 3, 4])

def apply_preset(name) =
  p = list.assoc(default=snd(list.hd(rp_presets)), name, rp_presets)
  p_drive := p.drive
  p_bass := p.bass
  p_presence := p.presence
  p_air := p.air
  p_limiter_drive := p.limiter_drive
  wide.threshold := p.wideband.threshold
  wide.ratio := p.wideband.ratio
  wide.gain := p.wideband.gain
  list.iteri(fun (i, b) -> begin
    cfg = list.nth(p.bands, i)
    b.threshold := cfg.threshold
    b.ratio := cfg.ratio
    b.gain := cfg.gain
  end, band_refs)
  current_preset := name
end

if not list.assoc.mem(current_preset(), rp_presets) then
  current_preset := fst(list.hd(rp_presets))
end
apply_preset(current_preset())

# Input level meter for the watchdog below.
music = peak(id="meter_in", duration=1., music)
s = music

# 1. Slow AGC: rides gain so quiet and loud masters come out similar.
#    up/down are time constants in seconds: rise slowly (no pumping), fall
#    faster. Not reset per track, like a broadcast AGC. Below -40 dB (fades,
#    silence) the gain is held so silence is not boosted.
s = if rp_agc and rp_processing_mode != "minimal" then
  normalize(target=rp_agc_target, up=10., down=1., window=1., gain_min=-10., gain_max=10.,
            threshold=-40., track_sensitive=false, s)
else
  s
end

# Filters are built from RBJ biquads. (filter.iir.butterworth fails to start
# on Liquidsoap 2.2.4.) A biquad with q=0.7071 is a 2nd-order Butterworth;
# two in series is a 4th-order Linkwitz-Riley.
def lp2(f, s) = filter.iir.eq.low(frequency=f, q=0.7071, s) end
def hp2(f, s) = filter.iir.eq.high(frequency=f, q=0.7071, s) end
def lr_low(f, s) = lp2(f, lp2(f, s)) end
def lr_high(f, s) = hp2(f, hp2(f, s)) end

# 8th-order Butterworth low-pass as four biquads with the standard Q values.
def lp8(f, s) =
  s = filter.iir.eq.low(frequency=f, q=0.5098, s)
  s = filter.iir.eq.low(frequency=f, q=0.6013, s)
  s = filter.iir.eq.low(frequency=f, q=0.9000, s)
  filter.iir.eq.low(frequency=f, q=2.5629, s)
end

def band_comp(b, attack, release, s) =
  compress(
    attack=attack, release=release, knee=3.,
    threshold={b.threshold()}, ratio={b.ratio()}, gain={b.gain()},
    s
  )
end

# 2-5, "full" mode: high-pass, 3-band EQ, four-band compressor. Bands are
# split with Linkwitz-Riley crossovers so they sum back close to flat.
def full_chain(s) =
  # Remove sub-sonic content the modulator cannot pass (spec: 30 Hz - 15 kHz).
  s = hp2(rp_highpass_hz, s)

  s = filter.iir.eq.peak(frequency=80., q=0.7, gain={p_bass()}, s)
  s = filter.iir.eq.peak(frequency=3000., q=1., gain={p_presence()}, s)
  s = filter.iir.eq.peak(frequency=10000., q=0.8, gain={p_air()}, s)

  s = amplify({lin_of_dB(p_drive())}, s)

  c1 = rp_crossovers.c1
  c2 = rp_crossovers.c2
  c3 = rp_crossovers.c3

  b1 = band_comp(list.nth(band_refs, 0), 40., 400., lr_low(c1, s))
  rest = lr_high(c1, s)
  b2 = band_comp(list.nth(band_refs, 1), 20., 250., lr_low(c2, rest))
  rest = lr_high(c2, rest)
  b3 = band_comp(list.nth(band_refs, 2), 10., 150., lr_low(c3, rest))
  b4 = band_comp(list.nth(band_refs, 3), 5., 100., lr_high(c3, rest))

  add(normalize=false, [b1, b2, b3, b4])
end

# 2-5, "lite" mode for slower boards (e.g. Raspberry Pi 2/3): high-pass,
# the same 3-band EQ, one wide-band compressor (the preset's "wideband").
def lite_chain(s) =
  s = hp2(rp_highpass_hz, s)
  s = filter.iir.eq.peak(frequency=80., q=0.7, gain={p_bass()}, s)
  s = filter.iir.eq.peak(frequency=3000., q=1., gain={p_presence()}, s)
  s = filter.iir.eq.peak(frequency=10000., q=0.8, gain={p_air()}, s)
  s = amplify({lin_of_dB(p_drive())}, s)
  band_comp(wide, 20., 250., s)
end

# "minimal" (for slow boards such as a Pi 3B): no AGC, EQ or compressor;
# the limiter, low-pass, pre-emphasis protection and ceiling below still run.
s = if rp_processing_mode == "lite" then lite_chain(s)
    elsif rp_processing_mode == "minimal" then s
    else full_chain(s) end
s = graph_cut(s)

ceiling = lin_of_dB(rp_ceiling_db)

def clip_at(level, s) =
  amplify(level, clip(amplify(1. / level, s)))
end

# 6. Limiter. The preset's limiter_drive pushes into it: more drive means
#    louder and denser. No lookahead: on Liquidsoap 2.2.4 compress() with
#    lookahead intermittently outputs NaN from startup on. The clip after it
#    catches the few milliseconds the limiter is too slow for.
s = amplify({lin_of_dB(p_limiter_drive())}, s)
s = compress(attack=1., release=80., knee=1., ratio=20., threshold=rp_ceiling_db - 2., s)
s = clip_at(lin_of_dB(rp_ceiling_db - 1.), s)

# 7. Keep audio, including clip distortion, out of the 19 kHz stereo pilot
#    (the modulator passes 15 kHz). Low-passing after the clip is why this
#    comes here and not earlier.
s = lp8(rp_lowpass_hz, s)

# 7b. Pre-emphasis protection. FM transmitters boost treble before modulating
#     (75 us in the US: +17 dB at 15 kHz), so bright material can over-deviate
#     even when the input level is fine. Limit the signal as the transmitter
#     will see it: apply the same emphasis, limit, then exactly undo it.
#
#     Built from one-pole low-passes (filter.rc; its rc is 1/frequency, and
#     its output is wetness*LP(x) + (1-wetness)*x):
#       emphasis    P(x) = (1+k)x - k*LP(tau_p)(x)   wetness -k, zero at tau_z
#       de-emphasis D(x) = x/(1+k) + k/(1+k)*LP(tau_z)(x)   exactly 1/P
#     with tau_p = tau_z/(1+k). rp_emphasis_tau/k are fitted (in generate.py)
#     so P is within 1 dB of the real curve from 200 Hz to 15 kHz.
#     (Mixing x with LP(x) via add() instead costs ~40% of a CPU core here.)
def emphasis(s) =
  k = rp_emphasis_k
  filter.rc(frequency=(1. + k) / rp_emphasis_tau, mode="low", wetness=0. - k, s)
end

def deemphasis(s) =
  k = rp_emphasis_k
  filter.rc(frequency=1. / rp_emphasis_tau, mode="low", wetness=k / (1. + k), s)
end

#     The limiter handles sustained treble; the clip catches fast transients
#     (cymbals, "s" sounds) it is too slow for. De-emphasis then turns down
#     the clip's distortion, and a second low-pass keeps it off the pilot.
s = if rp_emphasis_tau > 0. then
  e = emphasis(s)
  # Emphasis leaves bass at ~unity gain, so these levels also cap bass: just
  # under the main ceiling keeps nearly all of its headroom. Treble peaks can
  # overshoot by ~1 dB after de-emphasis and the low-pass.
  e = compress(attack=0.5, release=40., knee=1., ratio=20., threshold=rp_ceiling_db - 1.5, e)
  e = clip_at(lin_of_dB(rp_ceiling_db - 0.5), e)
  lp8(rp_lowpass_hz, deemphasis(e))
else
  s
end

# Pre-clip meter for the watchdog (the clip would hide a NaN as silence).
s = peak(id="meter_out", duration=1., s)
meter_out = s

# 8. Safety clip: the low-pass can overshoot the first clip by a dB or so;
#    nothing above the ceiling ever reaches the DAC.
s = clip_at(ceiling, s)

# Calibration tone: a 400 Hz sine exactly at the ceiling, i.e. the loudest
# the processed audio can ever be. Inserted after processing so nothing alters
# its level; the output trim below still applies. 400 Hz because many
# multimeters only read AC voltage accurately up to a few hundred Hz. Only
# reachable via the CLI/HTTP API, not Home Assistant, so it cannot go on air
# by accident.
tone = tag("calibration_tone", amplify(ceiling, sine(400.)))
s = switch(id="tone_switch", track_sensitive=false, [({tone_on()}, tone), ({true}, s)])

# 9. Output trim (the HA volume). Always <= max_volume_db, applied after the
#    clip, so peaks at the DAC are at most ceiling + max_volume_db.
s = amplify({lin_of_dB(volume_db())}, s)

# --------------------------------------------------------------------------
# Watchdog: if the processing chain ever produces NaN, or goes silent while
# music is coming in, exit so the supervisor (systemd or Docker) restarts
# the engine.
# --------------------------------------------------------------------------

dead_seconds = ref(0)

# True for NaN and infinities. Written as a range check because NaN
# comparisons differ between Liquidsoap 2.2 and 2.3.
def not_finite(x) =
  not (x >= 0. and x < 1000000.)
end

def watchdog() =
  i = music.peak()
  o = meter_out.peak()
  if not_finite(o) then
    log.severe(label="watchdog", "Processing output is NaN; restarting engine")
    shutdown(code=2)
  elsif i > 0.01 and o < 0.0001 then
    dead_seconds := dead_seconds() + 1
    if dead_seconds() >= rp_watchdog_seconds then
      log.severe(label="watchdog", "Input has audio but output is silent for #{dead_seconds()}s; restarting engine")
      shutdown(code=2)
    end
  else
    dead_seconds := 0
  end
end
thread.run(delay=5., every=1., watchdog)

# --------------------------------------------------------------------------
# Now playing
# --------------------------------------------------------------------------

now = ref({title = "", artist = "", album = "", filename = "", source = ""})

def on_meta(m) =
  now := {
    title = m["title"],
    artist = m["artist"],
    album = m["album"],
    filename = m["filename"],
    source = m["rp_source"]
  }
end
# Read song info where the playlists hand songs over, not at the output: on
# Liquidsoap 2.3 metadata doesn't reliably pass through the crossfade plus
# the graph-cut buffers, which left "now playing" stuck on an old song. This
# reports a song a second or two before it is heard.
music_fallback.on_metadata(on_meta)

# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

if rp_output == "alsa" then
  # Defined in generated.liq: the right call for this Liquidsoap version.
  rp_output_alsa(s)
elsif rp_output == "wav" then
  # For testing and measurement: record exactly what would go to the DAC.
  output.file(id="out", %wav(stereo=true, samplesize=24), rp_output_file, s)
else
  # "dummy" is for testing without a sound card.
  output.dummy(id="out", s)
end

# --------------------------------------------------------------------------
# HTTP control API (localhost only by default; used by the MQTT bridge)
# --------------------------------------------------------------------------

def json_response(data) =
  http.response(content_type="application/json", data=data)
end

def error_response(code, msg) =
  http.response(status_code=code, content_type="application/json",
                data=json.stringify(compact=true, {error = msg}))
end

def status_json() =
  n = now()
  json.stringify(compact=true, {
    playlist = current_playlist(),
    playlists = list.map(fst, playlist_sources),
    preset = current_preset(),
    presets = list.map(fst, rp_presets),
    volume_db = volume_db(),
    min_volume_db = rp_min_volume_db,
    max_volume_db = rp_max_volume_db,
    tone = tone_on(),
    title = n.title,
    artist = n.artist,
    album = n.album,
    filename = n.filename,
    source = n.source
  })
end

def h_status(_) =
  json_response(status_json())
end

def h_skip(_) =
  log.important(label="radioplayer", "Skip requested")
  music_fallback.skip()
  json_response(status_json())
end

def h_playlist(req) =
  name = list.assoc(default="", "name", req.query)
  if not playlist_exists(name) then
    error_response(404, "unknown playlist: #{name}")
  else
    if name != current_playlist() then
      log.important(label="radioplayer", "Switching playlist to #{name}")
      # Start the new playlist on a fresh song rather than resuming
      # wherever it was left the last time it was selected.
      source.skip(list.assoc(name, playlist_sources))
      current_playlist := name
      save_state()
    end
    json_response(status_json())
  end
end

def h_volume(req) =
  # -1000 is a sentinel for "not a number"; real values are clamped anyway.
  v = string.to_float(default=-1000., list.assoc(default="", "db", req.query))
  if v == -1000. then
    error_response(400, "db must be a number")
  else
    volume_db := clamp(rp_min_volume_db, rp_max_volume_db, v)
    save_state()
    json_response(status_json())
  end
end

def h_preset(req) =
  name = list.assoc(default="", "name", req.query)
  if not list.assoc.mem(name, rp_presets) then
    error_response(404, "unknown preset: #{name}")
  else
    log.important(label="radioplayer", "Processing preset #{name}")
    apply_preset(name)
    save_state()
    json_response(status_json())
  end
end

# Re-read every folder now instead of waiting for the hourly reload.
def h_reload(_) =
  log.important(label="radioplayer", "Reloading all folders")
  list.iter(fun (f) -> f(), reloaders())
  json_response(status_json())
end

# Exit so the supervisor (systemd or Docker) restarts the engine, which
# regenerates its settings from config.yaml on the way up. Used by "Rescan
# music library" when folders were added or removed.
def h_restart(_) =
  log.important(label="radioplayer", "Restart requested")
  thread.run(delay=0.5, fun () -> shutdown(code=0))
  json_response(status_json())
end

def h_tone(req) =
  on = list.assoc(default="", "on", req.query)
  tone_on := (on == "1" or on == "true" or on == "on")
  log.important(label="radioplayer", "Calibration tone #{tone_on()}")
  json_response(status_json())
end

harbor.http.register.simple(port=rp_http_port, method="GET", "/status", h_status)
harbor.http.register.simple(port=rp_http_port, method="POST", "/skip", h_skip)
harbor.http.register.simple(port=rp_http_port, method="POST", "/playlist", h_playlist)
harbor.http.register.simple(port=rp_http_port, method="POST", "/volume", h_volume)
harbor.http.register.simple(port=rp_http_port, method="POST", "/preset", h_preset)
harbor.http.register.simple(port=rp_http_port, method="POST", "/tone", h_tone)
harbor.http.register.simple(port=rp_http_port, method="POST", "/reload", h_reload)
harbor.http.register.simple(port=rp_http_port, method="POST", "/restart", h_restart)

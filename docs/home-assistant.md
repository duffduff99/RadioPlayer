# Home Assistant setup

## 1. MQTT broker

1. Settings → Add-ons → install **Mosquitto broker** and start it.
2. Settings → People → Users → add a user, e.g. `radioplayer`, with a password.
   (Mosquitto accepts any HA user.)
3. Settings → Devices & services: the **MQTT** integration should be
   discovered. Configure it.
4. On the Pi, in `/etc/radioplayer/config.yaml`:
   ```yaml
   mqtt:
     host: homeassistant.local   # or the HA IP address
     username: radioplayer
     password: the-password
   ```
   Then `sudo systemctl restart radioplayer-bridge`.

The device **FM Radio Player** appears under the MQTT integration within a
few seconds. Entity IDs are derived from the device and entity names, e.g.
`select.fm_radio_player_playlist`. Check yours under the device page, and
adjust the examples below if they differ.

## 2. Dashboard card

```yaml
type: vertical-stack
cards:
  - type: entities
    title: FM Radio
    entities:
      - entity: sensor.fm_radio_player_now_playing
        name: Now playing
      - entity: select.fm_radio_player_playlist
      - entity: number.fm_radio_player_output_level
      - entity: select.fm_radio_player_processing
      - entity: binary_sensor.fm_radio_player_playing_fallback
        name: On fallback
  - type: button
    entity: button.fm_radio_player_skip_song
    name: Skip
    icon: mdi:skip-next
    tap_action:
      action: perform-action
      perform_action: button.press
      target:
        entity_id: button.fm_radio_player_skip_song
```

### As a media player

To show the station as a media player (now playing, skip, playlist as the
source), add a [universal media player](https://www.home-assistant.io/integrations/universal/)
to `configuration.yaml`. If you already have a `media_player:` or `template:` section,
add the entries to it rather than a second key with the same name. The card
shows the song and the skip button only while the state is `playing`, which
the template sensor provides. Restart Home Assistant afterwards.

```yaml
template:
  - sensor:
      # "playing" while the engine is reachable, "off" when it isn't.
      - name: FM Radio state
        unique_id: fm_radio_player_media_state
        state: >
          {{ 'off' if states('select.fm_radio_player_playlist') in ['unavailable', 'unknown']
             else 'playing' }}

media_player:
  - platform: universal
    name: FM Radio
    unique_id: fm_radio_player_media
    device_class: receiver
    commands:
      media_next_track:
        action: button.press
        target:
          entity_id: button.fm_radio_player_skip_song
      select_source:
        action: select.select_option
        target:
          entity_id: select.fm_radio_player_playlist
        data:
          option: "{{ source }}"
    attributes:
      state: sensor.fm_radio_state
      media_title: sensor.fm_radio_player_now_playing|title
      media_artist: sensor.fm_radio_player_now_playing|artist
      media_album_name: sensor.fm_radio_player_now_playing|album
      source: select.fm_radio_player_playlist
      source_list: select.fm_radio_player_playlist|options
```

Then on a dashboard:

```yaml
type: media-control
entity: media_player.fm_radio
```

The output level is deliberately left out. It sets the transmitter's
modulation (calibrated in README section 5), not how loud the radios play,
so a volume slider on a media card would invite turning the station down by
accident. Use the output level entity for that instead.

Songs without tags have no title, so the card shows no title for them. The
"Now playing" sensor still shows their file name.

## 3. Automations

### Alert when the station is on fallback

The binary sensor turns on after the requested playlist hasn't been playing
for 20 s, e.g. when the SMB share is unreachable and the emergency folder is
on air.

```yaml
alias: FM radio on fallback
triggers:
  - trigger: state
    entity_id: binary_sensor.fm_radio_player_playing_fallback
    to: "on"
    for: "00:02:00"
actions:
  - action: notify.notify
    data:
      title: FM radio
      message: >
        Playing from {{ states('sensor.fm_radio_player_playing_from') }}
        instead of {{ states('select.fm_radio_player_playlist') }}.
        Check the music share.
```

### Alert when the player is offline

```yaml
alias: FM radio player offline
triggers:
  - trigger: state
    entity_id: select.fm_radio_player_playlist
    to: unavailable
    for: "00:02:00"
actions:
  - action: notify.notify
    data:
      message: The FM radio player is offline (Pi down, engine or bridge stopped).
```

Note: "offline" means HA lost contact with the player. The audio may
still be playing (for example when only the bridge or the broker is down).

### Holiday music in December

```yaml
alias: FM radio holiday playlist
triggers:
  - trigger: time
    at: "00:00:00"
conditions: []
actions:
  - action: select.select_option
    target:
      entity_id: select.fm_radio_player_playlist
    data:
      option: >
        {% if now().month == 12 %}Holiday Mix{% else %}All Music{% endif %}
```

## 4. MQTT topics (for other tools)

Base topic `radioplayer` by default (`mqtt.base_topic`).

| Topic | Direction | Payload |
|---|---|---|
| `radioplayer/state` | out, retained | JSON: playlist, playlists, preset, presets, volume_db, title, artist, album, filename, source, now_playing, fallback |
| `radioplayer/availability` | out, retained | `online` / `offline` (bridge, with LWT) |
| `radioplayer/engine` | out, retained | `online` / `offline` (audio engine reachable) |
| `radioplayer/playlist/set` | in | playlist name |
| `radioplayer/volume/set` | in | dB, e.g. `-6` |
| `radioplayer/preset/set` | in | preset name |
| `radioplayer/skip/press` | in | anything |
| `radioplayer/rescan/press` | in | anything |

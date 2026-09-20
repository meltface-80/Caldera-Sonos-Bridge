# Caldera Sonos Bridge

**Play your Plex music library to Sonos speakers, from Plexamp.** — v0.1.0

[Caldera Music headless](https://caldera.homes/music/headless/) is a Plex-powered music
daemon for Linux: you install it on an always-on machine, link it to your Plex account, and it
appears in Plexamp as something to play to. What it plays to is a sound card — ALSA, a DAC, an
amplifier. It has no Sonos output, and it cannot have one: Sonos speakers are not audio devices,
they are networked players that fetch their own streams.

This is the same idea with the other end swapped. It is a headless, Plex-controlled music daemon
for Linux, but instead of driving a sound card it drives **your Sonos speakers**. Every Sonos room
in the house appears in Plexamp as its own player — **Kitchen**, **Study**, and the rest — and
choosing one plays your Plex library on that speaker.

Solely Sonos. Nothing else is bridged, and nothing else needs to be.

---

## How it works

Plexamp cannot cast to a UPnP renderer, so a Sonos speaker is invisible to it. It *can* cast to a
Plex player, which is a much smaller thing than it sounds: a device that announces itself on the
network and answers a handful of HTTP requests. The bridge is one of those per room.

```
Plexamp  ──Plex Companion──▶  bridge  ──Sonos UPnP──▶  Sonos players
   │                                                        ▲
   └────── audio streamed straight from Plex Media Server ───┘
```

When you press play, the bridge asks your Plex Media Server what is in the play queue, turns each
track into a URL, and hands those URLs to the speaker. **The audio never passes through the
bridge.** Sonos fetches it directly from Plex, so there is no extra hop, no re-encoding, and
nothing for the bridge to be a bottleneck for. It is a remote control that speaks both dialects.

The parts worth knowing about:

- **Playback goes through the Sonos queue.** A window of the Plex queue is loaded onto the
  speaker, which then moves between tracks itself — that is what makes playback gapless, and what
  makes a skip instant rather than a round trip. The window is topped up as playback advances, so
  starting a 200-track playlist is not a visible wait.
- **Transport follows the group coordinator.** Playing to a grouped room plays to the group, which
  is how Sonos behaves. Volume and mute stay with the individual speaker.
- **State flows back.** The bridge reconciles with what the speaker is actually doing and reports
  it to Plex as a timeline, so Plexamp's now-playing screen stays honest even when someone pauses
  the music from the Sonos app. Progress is reported to your server too, so play counts and resume
  points work as they should.
- **Each room is a separate player** with a stable identity, so Plexamp remembers your speakers
  across restarts.

## Install

Run it on an always-on Linux machine on the same network as your speakers — a NAS, a Raspberry Pi,
a home server.

If Docker is not installed yet:

```bash
dietpi-software install 162              # DietPi (162 is its Docker package)
curl -fsSL https://get.docker.com | sh   # Debian, Ubuntu, Raspberry Pi OS
```

Then start the bridge:

```bash
docker run -d \
  --name caldera-sonos-bridge \
  --network host \
  --restart unless-stopped \
  -v caldera-sonos:/config \
  ghcr.io/meltface-80/caldera-sonos-bridge:latest
```

That is the whole installation. Open `http://<host-ip>:32700/` — the settings page lists the rooms
it has found, and has a **Link a Plex account** button. Click it, enter the four-character code it
gives you at [plex.tv/link](https://plex.tv/link), and your rooms appear in Plexamp.

> **`--network host` is required.** Plex's discovery protocol is multicast, and multicast does not
> cross Docker's default bridge network. Host networking is what lets Plex clients find your rooms
> and lets the bridge find your speakers. Docker Desktop for macOS and Windows does not provide
> real host networking, so the container needs a Linux host.

> **`-v caldera-sonos:/config` is worth keeping.** It holds the Plex token, your saved settings and
> the port each room was given. Without it you re-link after every image pull.

### Linking from the terminal instead

If you would rather not use the page — the same one-off step as `caldera-music --login`:

```bash
docker run --rm -it -v caldera-sonos:/config \
  ghcr.io/meltface-80/caldera-sonos-bridge:latest --login
```

**Why linking matters.** A Plex player is found in one of two ways, and they are not equivalent:

| | |
| --- | --- |
| **GDM** | A Plex-flavoured multicast search on your network. Desktop Plexamp and Plex Web use it. Nothing leaves the LAN, and no account is involved. Works without linking. |
| **plex.tv** | The room is registered to your Plex account, which hands out the local address to reach it on. **Plexamp on iOS and Android uses only this** — it sends no multicast at all. |

So on a phone, linking is the only way your rooms appear. Linking puts each room's *identity* in
your account, once; playback and control still go straight to the bridge over your own network
afterwards, and audio never touches plex.tv either way.

## The settings page

`http://<host-ip>:32700/` shows what the bridge can see and lets you change how it behaves —
name suffix, playback mode, stream format, volume ceiling, which rooms to publish — without
rewriting the `docker run` line. Settings are written to the config volume, so they survive the
container being replaced on the next image pull.

Anything you set there is badged **saved** and overrides the environment variable of the same name;
**reset** drops it and the environment's value applies again. Ports are the exception: they are set
from the environment only, because changing one needs a restart.

## Ports

| Port | Purpose |
| --- | --- |
| `32700/tcp` | The settings page and `/status.json` |
| `32600/tcp` and up | One Plex Companion player per room, counting up as rooms are found |
| `32412/udp` | GDM — how Plex clients on the network find the rooms |

Each room needs a port of its own because a Plex controller identifies a player by the address it
answers on; one port cannot be two players. A room keeps its port across restarts, so the addresses
published to your account stay valid.

## Configuration

Everything is optional; pass any of these with `-e NAME=value`. The ones marked ● can also be
changed on the settings page, which then takes precedence.

| Variable | Default | | Purpose |
| --- | --- | --- | --- |
| `NAME_SUFFIX` | `" (Sonos)"` | ● | Appended to each room name in Plexamp. Set to `""` for bare room names. |
| `HTTP_PORT` | `32700` | | Port for the settings page. |
| `PLAYER_PORT_BASE` | `32600` | | First port for the per-room players. |
| `PLEX_TOKEN` | — | | A Plex token, if you would rather supply one than link interactively. |
| `SONOS_HOSTS` | — | ● | Comma-separated player IPs, for when multicast discovery is unreliable. One is enough — the rest are read from the topology, and setting it also skips the discovery wait at start-up. |
| `INCLUDE_ZONES` | — | ● | Publish only these rooms, e.g. `Kitchen,Study`. |
| `EXCLUDE_ZONES` | — | ● | Publish everything except these rooms. |
| `BRIDGE_MODE` | `queue` | ● | `queue` for gapless playback via the Sonos queue; `direct` loads each track straight onto the transport. |
| `STREAM_FORMAT` | `original` | ● | `original` is bit-perfect within 24/48 and resamples above it — see [Formats](#formats-and-what-reaches-the-speaker). `flac` or `mp3` transcode everything. |
| `MAX_BITRATE_KBPS` | `0` | ● | Ceiling when transcoding to MP3. `0` means none. |
| `VOLUME_LIMIT` | `100` | ● | What Plex's slider at 100% sets the speaker to. |
| `UNGROUP_ON_PLAY` | `false` | ● | Detach a room from its Sonos group before playing to it. |
| `GDM_ENABLED` | `true` | | Answer Plex's multicast searches. Turn off only if something else on the host owns UDP 32412. |
| `BRIDGE_IP` | auto | | Address to advertise, for hosts with several interfaces. |
| `CONFIG_DIR` | `/config` | | Where the token, settings and port assignments live. |
| `DISCOVERY_INTERVAL` | `60` | ● | Seconds between searches for new players. |
| `TOPOLOGY_INTERVAL` | `30` | ● | Seconds between topology refreshes (new rooms, renames, regrouping). |
| `POLL_INTERVAL` | `5` | ● | Seconds between reconciliation polls against the speakers. |
| `LOG_LEVEL` | `INFO` | ● | `DEBUG` logs every Plex command and Sonos action. |

Example:

```bash
docker run -d --name caldera-sonos-bridge --network host --restart unless-stopped \
  -v caldera-sonos:/config \
  -e NAME_SUFFIX="" \
  -e EXCLUDE_ZONES="Bathroom,Garage" \
  -e VOLUME_LIMIT=80 \
  ghcr.io/meltface-80/caldera-sonos-bridge:latest
```

> **Bind mounts and permissions.** The bridge runs as uid `10001`, and a named volume inherits that
> ownership automatically. If you would rather bind-mount a host directory, either
> `chown -R 10001:10001 ./config` first or run the container with `--user "$(id -u):$(id -g)"`.

## Formats, and what reaches the speaker

Sonos S2 hardware plays FLAC, ALAC, WAV, AIFF, MP3, AAC and Ogg over HTTP, up to **24-bit/48 kHz**
— and does not play DSD at all. The bridge's default (`STREAM_FORMAT=original`) follows one rule:

| Your file | What the speaker gets |
| --- | --- |
| 16/44.1, 16/48, 24/44.1, 24/48 | **The stored file, untouched — bit-perfect.** Nothing decodes, resamples or re-encodes anywhere between the library and the speaker. |
| Above 24/48 — 24/88.2, 24/96, 24/192 | **Resampled to 24/48 and still lossless FLAC.** |
| A container Sonos cannot read — DSD, WMA lossless | Transcoded to FLAC within the ceiling. |

Hi-res is brought down rather than dropped to MP3 on purpose: losing the sample rate above 48 kHz
is a far smaller loss than losing the lossless coding, and refusing to play the track is no use to
anyone. The resample happens **on your Plex server**, which is the only thing in the chain that can
do it — the bridge tells it the ceiling and never touches the audio.

A file whose rate and depth Plex does not report is treated as within the ceiling and sent as-is.
That is deliberate: most libraries are ordinary CD resolution, and assuming the worst would
transcode a whole library that never needed it.

`STREAM_FORMAT=flac` or `mp3` forces *everything* through the transcoder, which is occasionally
useful on a slow network but costs you the bit-perfect path.

## Troubleshooting

Start at `http://<host-ip>:32700/`, which shows exactly what the bridge can see.

**The rooms table is empty.** The bridge has not found any players. Check that the container really
is on host networking, that the host shares a subnet with the speakers, and that nothing else on
the host holds UDP 1900 (`ss -lunp | grep 1900`). Setting `SONOS_HOSTS` to one player's IP address
skips discovery entirely.

**Rooms are listed, but Plexamp does not show them.** On a phone, that is almost always the account
link — check the settings page says *Linked*. On a desktop, it is the multicast path: Plexamp must
be on the same subnet, and some routers and access points filter multicast between wired and
wireless clients (look for IGMP snooping or "multicast enhancement" settings). The page tells you
whether GDM is running.

**The room says it could not read something from Plex.** The message names what the server
actually said. `HTTP 401` is a token your controller no longer has rights for — re-link, or restart
Plexamp. `HTTP 404` is a play queue the server has already forgotten, which happens if playback was
started a long time before the speaker was told about it; press play again. Anything else is worth
a look at the log with `LOG_LEVEL=DEBUG`, which records every request the bridge makes (with the
token redacted).

**Playback starts then stops.** Usually a format Sonos will not take. Run with `LOG_LEVEL=DEBUG`
and look for a UPnP error 714 (illegal MIME type), or try `STREAM_FORMAT=mp3` to confirm it is the
format rather than the plumbing.

**Playing to one room plays everywhere.** The room is grouped in the Sonos app, and transport
commands belong to the group coordinator. Ungroup it, or set `UNGROUP_ON_PLAY=true`.

**Two rooms fight over the same port.** Delete `ports.json` from the config volume and restart; the
assignments are rebuilt.

## Building and developing

```bash
# Build the image yourself
docker build -t caldera-sonos-bridge .
docker run -d --name caldera-sonos-bridge --network host \
  -v caldera-sonos:/config caldera-sonos-bridge

# Run it straight from a checkout
pip install -r requirements.txt
python -m calderabridge

# Tests: unit coverage plus an end-to-end run of the real bridge against a
# simulated Sonos household and a simulated Plex Media Server
pip install -r requirements-dev.txt
python -m pytest
```

Layout: `calderabridge/player.py` holds the translation between a Plex player and a Sonos room,
`calderabridge/companion.py` the HTTP surface a Plex controller talks to, `calderabridge/gdm.py`
discovery, `calderabridge/plexapi.py` the Plex Media Server client, `calderabridge/plexauth.py` the
account linking, `calderabridge/sonos.py` the Sonos dialect, `calderabridge/web.py` the settings
page, and `calderabridge/bridge.py` wires it together.

The Sonos half — the dialect, the topology handling, the device icons — is shared with
[UPnP-to-Sonos-UPnP-bridge](https://github.com/meltface-80/UPnP-to-Sonos-UPnP-bridge), which does
the same job for Audirvana and anything else that speaks plain UPnP.

## License

GPL-3.0 — see [LICENSE](LICENSE). Not affiliated with Sonos, Inc., Plex GmbH, or Caldera.

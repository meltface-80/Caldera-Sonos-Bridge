# Caldera Sonos Bridge

**Play your Plex music library to Sonos speakers, from Plexamp.** — v0.1.0

**📖 Install guide & docs: [meltface-80.github.io/Caldera-Sonos-Bridge](https://meltface-80.github.io/Caldera-Sonos-Bridge/)**

Every Sonos room in the house appears in Plexamp as its own player — **Kitchen**, **Study** and the
rest. Pick one, and your Plex library plays on that speaker.

---

## Why this exists

Plex used to play to Sonos itself. As of a few days ago it stopped.

The official integration — the one you set up through your Plex account — no longer works, and it
is not the only service to have dropped Sonos support recently. That left a good Plex library and a
house full of Sonos speakers with nothing joining them.

This bridge is that join, and it depends on nothing outside your own network: your Plex server,
your speakers, and a small daemon on a machine you own.

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

That is the whole installation. Then:

1. Open `http://<host-ip>:32700/` — the settings page lists the Sonos rooms it has found.
2. Click **Link a Plex account** and enter the four-character code at
   [plex.tv/link](https://plex.tv/link).
3. Open Plexamp. Your rooms are in the list of things to play to.

> **`--network host` is required.** Plex finds players by multicast, and multicast does not cross
> Docker's default bridge network. It is also how the bridge finds your speakers. Docker Desktop
> for macOS and Windows has no real host networking, so this needs a Linux host.

> **Keep the volume.** `/config` holds your Plex token, your settings and the port each room was
> given. Without it you re-link after every image pull.

Linking is only needed for **Plexamp on a phone**, which finds players through your Plex account
rather than over the network. Desktop Plexamp and Plex Web find the rooms without it. Either way,
playback goes straight to the bridge over your own network — nothing streams through plex.tv.

## How it works

Plexamp cannot cast to a Sonos speaker, but it can cast to a Plex player — which is a much smaller
thing than it sounds: a device that announces itself and answers a handful of HTTP requests. The
bridge is one of those per room.

```
Plexamp  ──Plex Companion──▶  bridge  ──Sonos UPnP──▶  Sonos players
   │                                                        ▲
   └────── audio streamed straight from Plex Media Server ───┘
```

Audio does not follow that path. The bridge hands each speaker a URL and Sonos fetches the music
straight from your Plex server — no extra hop, nothing re-encoded in passing, and nothing for the
bridge to be a bottleneck for.

A window of the Plex queue is loaded onto the speaker, which then moves between tracks itself, so
playback is gapless and skips are instant. Playing to a grouped room plays to the group, as Sonos
does. Pause from the Sonos app and Plexamp's now-playing screen follows; progress goes back to your
server, so play counts and resume points work.

## Formats

| Your file | What the speaker gets |
| --- | --- |
| 16/44.1, 16/48, 24/44.1, 24/48 | The stored file, untouched — **bit-perfect** |
| Above 24/48 | Resampled to 24/48 by your Plex server, still **lossless FLAC** |
| A container Sonos cannot read — DSD, WMA lossless | Transcoded within the same ceiling |

Sonos S2 hardware plays up to 24-bit/48 kHz. Hi-res is brought down rather than dropped to MP3 on
purpose: losing the sample rate above 48 kHz is a far smaller loss than losing lossless coding. The
resample happens on your Plex server — the bridge never touches the audio. If your server will not
serve lossless, the bridge falls back to 320 kbps MP3 and says so in the log.

## Settings

`http://<host-ip>:32700/` shows every room and what it is doing, and lets you change how the bridge
behaves without rewriting the `docker run` line. Settings are written to the config volume, so they
survive the container being replaced.

Everything below is optional; pass any of it with `-e NAME=value`. The ones marked ● can also be
set on the settings page, which then takes precedence.

| Variable | Default | | Purpose |
| --- | --- | --- | --- |
| `NAME_SUFFIX` | `" (Sonos)"` | ● | Appended to each room name in Plexamp. Empty for bare room names. |
| `HTTP_PORT` | `32700` | | Port for the settings page. |
| `PLAYER_PORT_BASE` | `32701` | | First port for the per-room players. Plex's own ports are skipped. |
| `SONOS_HOSTS` | — | ● | A player's IP, for when multicast discovery is unreliable. One is enough. |
| `INCLUDE_ZONES` | — | ● | Publish only these rooms, e.g. `Kitchen,Study`. |
| `EXCLUDE_ZONES` | — | ● | Publish everything except these rooms. |
| `STREAM_FORMAT` | `original` | ● | Bit-perfect within 24/48, resampled above it. `flac` or `mp3` transcode everything. |
| `MAX_BITRATE_KBPS` | `0` | ● | Ceiling when transcoding to MP3. `0` means none. |
| `VOLUME_LIMIT` | `100` | ● | What Plex's slider at 100% sets the speaker to. |
| `UNGROUP_ON_PLAY` | `false` | ● | Detach a room from its Sonos group before playing to it. |
| `BRIDGE_MODE` | `queue` | ● | `queue` for gapless playback; `direct` loads each track onto the transport. |
| `PLEX_TOKEN` | — | | A Plex token, if you would rather supply one than link interactively. |
| `PLEX_VERIFY_SSL` | `true` | | Check your Plex server's certificate. Only used if the bridge has to fall back to HTTPS. |
| `BRIDGE_IP` | auto | | Address to advertise, for hosts with several interfaces. |
| `LOG_LEVEL` | `INFO` | ● | `DEBUG` logs every Plex command and Sonos action. |

Ports: `32700/tcp` the settings page, `32701/tcp` and up one player per room, `32412/udp` for Plex's
discovery. The rooms start above `32700` because the ports below are Plex's own — `32400` the
server, `32410`–`32414` discovery, `32469` DLNA, `32600` the Tuner Service.

## Troubleshooting

Start at `http://<host-ip>:32700/`, which shows exactly what the bridge can see.

**The rooms table is empty.** No players found. Check the container really is on host networking and
shares a subnet with the speakers. Setting `SONOS_HOSTS` to one player's IP skips discovery
entirely.

**Rooms are listed, but Plexamp shows nothing to cast to.** On a phone, check the **On Plex** column
says *published*. On a desktop it is the multicast path — same subnet, and watch for IGMP snooping
or "multicast enhancement" on routers and access points.

**Playback starts then stops.** Usually a format Sonos will not take. Run with `LOG_LEVEL=DEBUG` and
look for UPnP error 714, or set `STREAM_FORMAT=mp3` to confirm it is the format rather than the
plumbing.

**Playing to one room plays everywhere.** The room is grouped in the Sonos app, and transport
commands belong to the group coordinator. Ungroup it, or set `UNGROUP_ON_PLAY=true`.

**A room's port is already in use.** The bridge moves it to the next free port and logs the move.
To see what holds one: `ss -lptn 'sport = :32701'`.

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

Layout: `calderabridge/player.py` translates between a Plex player and a Sonos room,
`companion.py` is the HTTP surface a Plex controller talks to, `gdm.py` discovery, `plexapi.py` the
Plex Media Server client, `plexauth.py` account linking, `sonos.py` the Sonos dialect, `web.py` the
settings page, and `bridge.py` wires it together.

The Sonos half — the dialect, the topology handling, the device icons — is shared with
[UPnP-to-Sonos-UPnP-bridge](https://github.com/meltface-80/UPnP-to-Sonos-UPnP-bridge), which does
the same job for Audirvana and anything else that speaks plain UPnP.

## Project page

The site under `docs/` is published with GitHub Pages: **Settings → Pages → Source: Deploy from a
branch → `main` / `/docs`**.

## License

GPL-3.0 — see [LICENSE](LICENSE). Not affiliated with Sonos, Inc., Plex GmbH, or Caldera.

"""The settings page, and the small JSON API behind it.

The sibling UPnP bridge serves a status page that answers "what can the bridge
see?".  This does the same, and adds the two things that only matter here: the
Plex account the rooms are published to, and settings that can be changed
without rewriting the ``docker run`` line.

Everything editable is written to the config volume, so it survives the
container being replaced on the next image pull.
"""

from __future__ import annotations

import html
import logging

from aiohttp import web

from .config import BRIDGE_NAME, BRIDGE_VERSION, EDITABLE, STREAM_FORMATS
from .icon import ICON_SIZES
from .icon import render as render_icon
from .speakers import label as speaker_label
from .speakers import svg as speaker_svg

LOGGER = logging.getLogger(__name__)

BRIDGE_KEY: web.AppKey = web.AppKey("bridge")


def create_app(bridge) -> web.Application:
    app = web.Application()
    app[BRIDGE_KEY] = bridge

    app.router.add_get("/", handle_page)
    app.router.add_get("/status.json", handle_status_json)
    app.router.add_post("/settings", handle_save)
    app.router.add_post("/settings/reset", handle_reset)
    app.router.add_post("/plex/link", handle_link)
    app.router.add_get("/plex/link", handle_link_status)
    app.router.add_post("/plex/unlink", handle_unlink)
    app.router.add_get("/room/{uid}/icon.svg", handle_icon_svg)
    app.router.add_get("/room/{uid}/icon/{size}.png", handle_icon_png)
    return app


def _bridge(request: web.Request):
    return request.app[BRIDGE_KEY]


def _player(request: web.Request):
    player = _bridge(request).player_for_zone(request.match_info["uid"])
    if player is None:
        raise web.HTTPNotFound(text="Unknown room")
    return player


# ----------------------------------------------------------------------
# JSON
# ----------------------------------------------------------------------
async def handle_status_json(request: web.Request) -> web.Response:
    return web.json_response(await _bridge(request).status())


async def handle_save(request: web.Request) -> web.Response:
    """Save settings, from either the form or a JSON body."""
    bridge = _bridge(request)
    if request.content_type == "application/json":
        payload = await request.json()
        updates = {k: v for k, v in payload.items() if k in EDITABLE}
    else:
        form = await request.post()
        updates = _from_form(form)

    changed = await bridge.apply_settings(updates)
    if request.content_type == "application/json":
        return web.json_response({"changed": sorted(changed)})
    raise web.HTTPFound("/?saved=" + str(len(changed)))


def _from_form(form) -> dict[str, object]:
    """Read the settings form.

    An unchecked checkbox sends nothing at all, so the booleans are read from
    the hidden marker the form posts alongside each one rather than from the
    presence of the key.
    """
    updates: dict[str, object] = {}
    for key in EDITABLE:
        if f"_present_{key}" not in form:
            continue
        if key in ("ungroup_on_play",):
            updates[key] = form.get(key, "") in ("on", "1", "true")
        else:
            updates[key] = form.get(key, "")
    return updates


async def handle_reset(request: web.Request) -> web.Response:
    form = await request.post()
    key = form.get("key", "")
    changed = await _bridge(request).reset_settings([key] if key else None)
    raise web.HTTPFound("/?reset=" + str(len(changed)))


async def handle_link(request: web.Request) -> web.Response:
    try:
        code = await _bridge(request).begin_link()
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=502)
    return web.json_response({"code": code.code, "url": code.url})


async def handle_link_status(request: web.Request) -> web.Response:
    return web.json_response(_bridge(request).link_status())


async def handle_unlink(request: web.Request) -> web.Response:
    await _bridge(request).unlink()
    raise web.HTTPFound("/")


# ----------------------------------------------------------------------
# Icons
# ----------------------------------------------------------------------
async def handle_icon_svg(request: web.Request) -> web.Response:
    player = _player(request)
    body = speaker_svg(
        player.icon_kind,
        player.stereo_pair,
        size=128,
        title=player.zone.model or speaker_label(player.icon_kind),
        auto_theme=True,
    )
    return web.Response(
        text=body, headers={"Content-Type": "image/svg+xml", "Cache-Control": "max-age=86400"}
    )


async def handle_icon_png(request: web.Request) -> web.Response:
    player = _player(request)
    try:
        size = int(request.match_info["size"])
    except ValueError as exc:
        raise web.HTTPNotFound(text="Unknown icon") from exc
    if size not in ICON_SIZES:
        raise web.HTTPNotFound(text="Unknown icon size")
    return web.Response(
        body=render_icon(size, player.icon_kind, player.stereo_pair),
        headers={"Content-Type": "image/png", "Cache-Control": "max-age=86400"},
    )


# ----------------------------------------------------------------------
# The page
# ----------------------------------------------------------------------
def _esc(value: object) -> str:
    return html.escape(str(value))


def _duration(ms: object) -> str:
    seconds = int(ms or 0) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def _rooms_table(status: dict) -> str:
    rows = []
    for room in status["rooms"]:
        icon = speaker_svg(
            str(room["iconKind"]),
            bool(room["stereoPair"]),
            size=30,
            title=str(room["model"]) or speaker_label(str(room["iconKind"])),
        )
        model = str(room["model"]) or "-"
        if room["stereoPair"]:
            model += " (stereo pair)"

        playing = room.get("nowPlaying")
        if playing:
            now = (
                f"<strong>{_esc(playing['title'])}</strong><br>"
                f"<span class=dim>{_esc(playing['artist'])}"
                f" &middot; {_duration(playing['positionMs'])}"
                f" / {_duration(playing['durationMs'])}</span>"
            )
        elif room["lastError"]:
            now = f"<span class=bad>{_esc(room['lastError'])}</span>"
        else:
            now = "<span class=dim>-</span>"

        rows.append(
            "<tr>"
            f'<td><span class=room><span class=glyph>{icon}</span>'
            f"<span><strong>{_esc(room['playerName'])}</strong>"
            f"<br><span class=dim>{_esc(model)}</span></span></span></td>"
            f"<td>{_esc(room['sonosIp'])}<br><span class=dim>port {room['port']}</span></td>"
            f"<td>{_esc(room['coordinator'])}</td>"
            f"<td><span class=\"pill {_esc(room['state'])}\">{_esc(room['state'])}</span></td>"
            f"<td>{now}</td>"
            f"<td>{room['volume']}{' (muted)' if room['mute'] else ''}</td>"
            "</tr>"
        )

    if not rows:
        rows.append(
            '<tr><td colspan="6">No Sonos rooms found yet. Check that the container '
            "runs with <code>--network host</code>, or set <code>SONOS_HOSTS</code> "
            "to one player's IP address.</td></tr>"
        )
    return "".join(rows)


def _field(name: str, label: str, control: str, note: str, overridden: bool) -> str:
    badge = ' <span class=badge title="Set here, not from the environment">saved</span>' if overridden else ""
    reset = (
        f'<form method=post action="/settings/reset" class=inline>'
        f'<input type=hidden name=key value="{_esc(name)}">'
        f"<button class=link type=submit>reset</button></form>"
        if overridden
        else ""
    )
    return (
        f'<div class=field><label for="{_esc(name)}">{_esc(label)}{badge}</label>'
        f"{control}<p class=note>{note} {reset}</p></div>"
    )


def _text_input(name: str, value: object, placeholder: str = "") -> str:
    return (
        f'<input type=text id="{_esc(name)}" name="{_esc(name)}" '
        f'value="{_esc(value)}" placeholder="{_esc(placeholder)}">'
        f'<input type=hidden name="_present_{_esc(name)}" value="1">'
    )


def _select(name: str, value: object, options: tuple[str, ...]) -> str:
    items = "".join(
        f'<option value="{_esc(option)}"{" selected" if str(value) == option else ""}>'
        f"{_esc(option)}</option>"
        for option in options
    )
    return (
        f'<select id="{_esc(name)}" name="{_esc(name)}">{items}</select>'
        f'<input type=hidden name="_present_{_esc(name)}" value="1">'
    )


def _checkbox(name: str, value: object) -> str:
    checked = " checked" if value else ""
    return (
        f'<label class=switch><input type=checkbox id="{_esc(name)}" '
        f'name="{_esc(name)}"{checked}><span></span></label>'
        f'<input type=hidden name="_present_{_esc(name)}" value="1">'
    )


def _settings_form(status: dict) -> str:
    settings = status["settings"]
    over = set(status["overridden"])

    def f(name, label, control, note):
        return _field(name, label, control, note, name in over)

    return "".join(
        [
            "<form method=post action='/settings' class=settings>",
            "<div class=grid>",
            f(
                "name_suffix",
                "Name suffix",
                _text_input("name_suffix", settings["name_suffix"], " (Sonos)"),
                "Appended to each room name in Plexamp. Leave empty for bare room names.",
            ),
            f(
                "mode",
                "Playback mode",
                _select("mode", settings["mode"], ("queue", "direct")),
                "<code>queue</code> gives gapless playback through the Sonos queue. "
                "<code>direct</code> loads each track onto the transport instead.",
            ),
            f(
                "stream_format",
                "Stream format",
                _select("stream_format", settings["stream_format"], STREAM_FORMATS),
                "<code>original</code> sends the file as Plex stores it - the right "
                "choice for a FLAC or MP3 library. Anything Sonos cannot play is "
                "transcoded regardless.",
            ),
            f(
                "max_bitrate_kbps",
                "Max bitrate (kbps)",
                _text_input("max_bitrate_kbps", settings["max_bitrate_kbps"], "0"),
                "Only applies when transcoding to MP3. <code>0</code> means no ceiling.",
            ),
            f(
                "volume_limit",
                "Volume ceiling",
                _text_input("volume_limit", settings["volume_limit"], "100"),
                "Plex's slider at 100% sets the speaker to this. Useful in a small room.",
            ),
            f(
                "ungroup_on_play",
                "Ungroup on play",
                _checkbox("ungroup_on_play", settings["ungroup_on_play"]),
                "Detach a room from its Sonos group before playing to it.",
            ),
            f(
                "include_zones",
                "Only these rooms",
                _text_input("include_zones", ", ".join(settings["include_zones"]), "Kitchen, Study"),
                "Comma-separated. Leave empty to publish every room.",
            ),
            f(
                "exclude_zones",
                "Except these rooms",
                _text_input("exclude_zones", ", ".join(settings["exclude_zones"]), "Bathroom"),
                "Comma-separated.",
            ),
            f(
                "static_hosts",
                "Sonos addresses",
                _text_input("static_hosts", ", ".join(settings["static_hosts"]), "192.168.1.40"),
                "For when multicast discovery is unreliable. One is enough - the rest "
                "are read from the household topology.",
            ),
            f(
                "log_level",
                "Log level",
                _select("log_level", settings["log_level"], ("DEBUG", "INFO", "WARNING", "ERROR")),
                "<code>DEBUG</code> logs every Plex command and Sonos action.",
            ),
            "</div>",
            "<div class=actions><button type=submit class=primary>Save settings</button>",
            "<form method=post action='/settings/reset' class=inline>"
            "<button type=submit class=link>reset everything to the environment</button></form>",
            "</div></form>",
        ]
    )


def _plex_card(status: dict) -> str:
    plex = status["plex"]
    if plex["linked"]:
        who = f" as <strong>{_esc(plex['username'])}</strong>" if plex["username"] else ""
        servers = plex.get("servers") or []
        server_line = (
            f"<p class=note>Sees {len(servers)} Plex server(s): "
            + ", ".join(_esc(s["name"]) for s in servers)
            + "</p>"
            if servers
            else ""
        )
        return (
            "<section class=card><h2>Plex account</h2>"
            f"<p class=ok>Linked{who}. Rooms are published to your account, so "
            "Plexamp on a phone can see them.</p>"
            f"{server_line}"
            "<form method=post action='/plex/unlink'>"
            "<button type=submit class=danger>Unlink this account</button></form></section>"
        )
    return (
        "<section class=card><h2>Plex account</h2>"
        "<p>Not linked. Desktop Plexamp and Plex Web will still find your rooms "
        "over the local network, but <strong>Plexamp on a phone only sees players "
        "registered to your account</strong>.</p>"
        "<div id=linkbox><button type=button class=primary id=linkbtn>Link a Plex account</button></div>"
        "</section>"
    )


PAGE_SCRIPT = """
const linkbtn = document.getElementById('linkbtn');
if (linkbtn) {
  linkbtn.addEventListener('click', async () => {
    linkbtn.disabled = true;
    linkbtn.textContent = 'Asking plex.tv\\u2026';
    const box = document.getElementById('linkbox');
    try {
      const res = await fetch('/plex/link', {method: 'POST'});
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || 'plex.tv would not issue a code');
      box.innerHTML = '<p>Open <a href="' + body.url + '" target="_blank" rel="noopener">' +
        body.url + '</a> and enter this code:</p><p class=code>' + body.code + '</p>' +
        '<p class=note id=linkstate>Waiting for the code to be entered\\u2026</p>';
      const poll = setInterval(async () => {
        const s = await (await fetch('/plex/link')).json();
        if (s.linked) { clearInterval(poll); location.reload(); }
        else if (s.error) {
          clearInterval(poll);
          document.getElementById('linkstate').textContent = s.error;
        }
      }, 2000);
    } catch (err) {
      box.innerHTML = '<p class=bad>' + err.message + '</p>';
    }
  });
}

// Keep the rooms table live without reloading the settings form under the user.
setInterval(async () => {
  if (document.activeElement && ['INPUT','SELECT'].includes(document.activeElement.tagName)) return;
  try {
    const status = await (await fetch('/status.json')).json();
    const table = document.getElementById('rooms');
    if (table && status.roomsHtml) table.innerHTML = status.roomsHtml;
  } catch (e) { /* the bridge is restarting; the next tick will do */ }
}, 4000);
"""

STYLE = """
 :root { color-scheme: light dark; --bg:#f6f7f9; --fg:#16181d; --card:#fff; --line:rgba(128,128,128,.22);
         --dim:rgba(90,95,105,.85); --accent:#e5a00d; --ok:#2e7d32; --bad:#c62828; }
 @media (prefers-color-scheme: dark) {
   :root { --bg:#14161a; --fg:#e8eaee; --card:#1d2026; --dim:rgba(200,205,215,.7); --ok:#81c784; --bad:#ef9a9a; } }
 * { box-sizing: border-box; }
 body { font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        margin: 0; padding: 2rem 1.25rem 3rem; background: var(--bg); color: var(--fg); }
 main { max-width: 64rem; margin: 0 auto; }
 h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
 h2 { font-size: .95rem; text-transform: uppercase; letter-spacing: .05em;
      opacity: .6; margin: 0 0 .9rem; font-weight: 600; }
 p.sub { margin: 0 0 1.75rem; color: var(--dim); }
 .card { background: var(--card); border-radius: 10px; padding: 1.25rem;
         box-shadow: 0 1px 3px rgba(0,0,0,.12); margin-bottom: 1.5rem; }
 table { border-collapse: collapse; width: 100%; }
 th, td { text-align: left; padding: .6rem .7rem; border-bottom: 1px solid var(--line);
          vertical-align: top; }
 th { font-size: .7rem; text-transform: uppercase; letter-spacing: .04em; opacity: .6; }
 tr:last-child td { border-bottom: 0; }
 code { background: rgba(128,128,128,.16); padding: .1rem .35rem; border-radius: 4px;
        font-size: .9em; }
 .room { display: flex; align-items: center; gap: .65rem; }
 .glyph { display: inline-flex; opacity: .85; flex: 0 0 auto; }
 .dim { color: var(--dim); font-size: .85em; }
 .ok { color: var(--ok); } .bad { color: var(--bad); }
 .pill { display: inline-block; padding: .12rem .5rem; border-radius: 999px;
         background: rgba(128,128,128,.18); font-size: .78rem; }
 .pill.playing { background: rgba(46,125,50,.2); color: var(--ok); }
 .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(17rem, 1fr)); gap: 1.4rem; }
 .field label { display: block; font-weight: 600; font-size: .85rem; margin-bottom: .35rem; }
 .field input[type=text], .field select { width: 100%; padding: .45rem .6rem; font: inherit;
        border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--fg); }
 .note { margin: .35rem 0 0; font-size: .78rem; color: var(--dim); }
 .badge { font-size: .65rem; text-transform: uppercase; letter-spacing: .04em;
          background: var(--accent); color: #241c00; padding: .05rem .35rem;
          border-radius: 3px; vertical-align: middle; font-weight: 700; }
 .switch input { width: 2.4rem; height: 1.3rem; }
 .actions { margin-top: 1.5rem; display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
 button { font: inherit; cursor: pointer; }
 button.primary { background: var(--accent); color: #241c00; border: 0; font-weight: 600;
          padding: .5rem 1rem; border-radius: 6px; }
 button.danger { background: transparent; color: var(--bad); border: 1px solid var(--bad);
          padding: .4rem .8rem; border-radius: 6px; }
 button.link { background: none; border: 0; color: var(--dim); text-decoration: underline;
          padding: 0; font-size: .78rem; }
 .inline { display: inline; }
 .code { font: 700 1.9rem/1 ui-monospace, SFMono-Regular, Menlo, monospace;
         letter-spacing: .35em; margin: .75rem 0; }
 .banner { background: rgba(46,125,50,.15); color: var(--ok); padding: .6rem .9rem;
           border-radius: 8px; margin-bottom: 1.25rem; font-size: .9rem; }
 footer { color: var(--dim); font-size: .82rem; margin-top: 2rem; }
 footer a { color: inherit; }
"""


async def handle_page(request: web.Request) -> web.Response:
    bridge = _bridge(request)
    status = await bridge.status()

    banner = ""
    if "saved" in request.query:
        count = request.query.get("saved", "0")
        banner = (
            f'<div class=banner>Saved. {count} setting(s) changed.</div>'
            if count != "0"
            else '<div class=banner>Saved - nothing needed changing.</div>'
        )
    elif "reset" in request.query:
        banner = "<div class=banner>Reset to the values from the environment.</div>"

    gdm = (
        "answering searches on the network"
        if status["gdm"]
        else "<span class=bad>not running - check host networking</span>"
    )

    page = f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{_esc(BRIDGE_NAME)}</title>
<link rel="icon" href="data:,">
<style>{STYLE}</style></head><body><main>
<h1>{_esc(BRIDGE_NAME)} <span style="opacity:.45">v{BRIDGE_VERSION}</span></h1>
<p class=sub>Publishing {len(status['rooms'])} Sonos room(s) to Plex as players from
<code>{_esc(status['bridgeIp'])}</code>, in <code>{_esc(status['settings']['mode'])}</code> mode.
GDM is {gdm}.</p>
{banner}

{_plex_card(status)}

<section class=card><h2>Rooms</h2>
<table><thead><tr>
<th>Player</th><th>Sonos</th><th>Group coordinator</th><th>State</th>
<th>Now playing</th><th>Volume</th></tr></thead>
<tbody id=rooms>{_rooms_table(status)}</tbody></table></section>

<section class=card><h2>Settings</h2>
{_settings_form(status)}
</section>

<footer>
Machine-readable status: <a href="/status.json">/status.json</a>.
Settings are stored in <code>{_esc(status['settingsPath'])}</code>; ports are set
from the environment and need a restart.
</footer>
</main><script>{PAGE_SCRIPT}</script></body></html>"""
    return web.Response(text=page, content_type="text/html")


def rooms_html(status: dict) -> str:
    """The rooms table body, for the page's own periodic refresh."""
    return _rooms_table(status)


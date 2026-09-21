"""Runtime configuration.

Two sources feed one :class:`Config`.  The environment supplies the defaults, as
it does for any container; the settings page writes ``settings.json`` into the
config volume, and that wins where it has an opinion.  The split is what lets a
setting be changed from a browser without rewriting the ``docker run`` line, and
lets "reset" mean something precise: drop the key from the file and the
environment's value applies again.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path

LOGGER = logging.getLogger(__name__)

BRIDGE_NAME = "Caldera Sonos Bridge"
BRIDGE_VERSION = "0.1.11"

# Stable namespace so a given Sonos player always maps to the same Plex machine
# identifier, across restarts and reinstalls.  Plex clients remember a player by
# that identifier; a new one each start would look like a new device each start,
# and the cast list would fill with ghosts.
UUID_NAMESPACE = uuid.UUID("2f4d7c61-9a0e-5b3f-8c72-1d6e4a9b0f35")

DEFAULT_CONFIG_DIR = "/config"
SETTINGS_FILENAME = "settings.json"

#: Ports.  32700 is the settings page, and each room gets its own Plex Companion
#: port counting up from just above it, because a Plex player is identified by
#: the endpoint it answers on - one port cannot be two players.
#:
#: Starting at 32701 rather than 32600 is deliberate.  This bridge usually runs
#: on the same machine as Plex Media Server, and 32600 is Plex's own: the Tuner
#: Service binds it.  The whole bridge therefore sits in one contiguous block
#: above Plex's range, which is easier to reason about and to firewall.
DEFAULT_SETTINGS_PORT = 32700
DEFAULT_PLAYER_PORT_BASE = 32701
DEFAULT_GDM_PORT = 32412

#: Ports Plex Media Server uses on the host it runs on.  The bridge keeps clear
#: of these by default; they are listed so a hand-set PLAYER_PORT_BASE that
#: lands on one can be called out at start-up rather than discovered by a bind
#: failing partway through.
PLEX_PORTS = {
    32400,  # the server itself
    32410, 32412, 32413, 32414,  # GDM
    32469,  # DLNA
    32600,  # Tuner Service (DVR)
    3005,  # Plex Companion
    8324,  # Roku control
}

#: Keys the settings page may write.  Anything outside this set is either a port
#: (changing it needs a restart, so it stays an environment variable) or derived.
EDITABLE = (
    "name_suffix",
    "mode",
    "ungroup_on_play",
    "include_zones",
    "exclude_zones",
    "static_hosts",
    "stream_format",
    "max_bitrate_kbps",
    "volume_limit",
    "log_level",
    "discovery_interval",
    "topology_interval",
    "poll_interval",
    "auto_update",
)

#: How a track reaches the speaker.  ``original`` hands Sonos the file as Plex
#: stores it, which is what you want when the library is already FLAC or MP3.
#: The others ask Plex to transcode on the way out, for libraries holding
#: formats Sonos will not take (DSD, 24/192, WMA lossless).
STREAM_FORMATS = ("original", "flac", "mp3")


def _str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None else value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    raw = _str(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _clean_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    return mode if mode in ("queue", "direct") else "queue"


def _clean_format(value: str) -> str:
    fmt = (value or "").strip().lower()
    return fmt if fmt in STREAM_FORMATS else "original"


@dataclass
class Config:
    """All tunables.  Every field maps to an environment variable, and the
    subset in :data:`EDITABLE` may also be set from the settings page."""

    # Networking -----------------------------------------------------------
    bridge_ip: str = ""
    settings_port: int = DEFAULT_SETTINGS_PORT
    player_port_base: int = DEFAULT_PLAYER_PORT_BASE
    gdm_port: int = DEFAULT_GDM_PORT
    ssdp_port: int = 1900
    multicast_ttl: int = 4
    gdm_enabled: bool = True

    # Plex -----------------------------------------------------------------
    config_dir: str = DEFAULT_CONFIG_DIR
    plex_token: str = ""
    name_suffix: str = " (Sonos)"
    stream_format: str = "original"
    max_bitrate_kbps: int = 0  # 0 = no ceiling; only applies when transcoding
    verify_ssl: bool = True

    # Behaviour ------------------------------------------------------------
    mode: str = "queue"  # "queue" (gapless, via the Sonos queue) or "direct"
    ungroup_on_play: bool = False
    volume_limit: int = 100
    include_zones: list[str] = field(default_factory=list)
    exclude_zones: list[str] = field(default_factory=list)
    static_hosts: list[str] = field(default_factory=list)

    # Timing ---------------------------------------------------------------
    discovery_interval: float = 60.0
    discovery_mx: int = 2
    discovery_attempts: int = 3
    topology_interval: float = 30.0
    poll_interval: float = 5.0
    sonos_sub_timeout: int = 600
    http_timeout: float = 10.0
    timeline_interval: float = 1.0

    # Updates --------------------------------------------------------------
    update_check: bool = True
    auto_update: bool = False
    update_check_interval: float = 21600.0  # six hours

    # Misc -----------------------------------------------------------------
    log_level: str = "INFO"

    #: Which keys came from ``settings.json`` rather than the environment.  The
    #: settings page uses this to show what has been overridden.
    overridden: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> Config:
        return cls(
            bridge_ip=_str("BRIDGE_IP", "").strip(),
            settings_port=_int("HTTP_PORT", DEFAULT_SETTINGS_PORT),
            player_port_base=_int("PLAYER_PORT_BASE", DEFAULT_PLAYER_PORT_BASE),
            gdm_port=_int("GDM_PORT", DEFAULT_GDM_PORT),
            ssdp_port=_int("SSDP_PORT", 1900),
            multicast_ttl=_int("MULTICAST_TTL", 4),
            gdm_enabled=_bool("GDM_ENABLED", True),
            config_dir=_str("CONFIG_DIR", DEFAULT_CONFIG_DIR),
            plex_token=_str("PLEX_TOKEN", "").strip(),
            name_suffix=_str("NAME_SUFFIX", " (Sonos)"),
            stream_format=_clean_format(_str("STREAM_FORMAT", "original")),
            max_bitrate_kbps=_int("MAX_BITRATE_KBPS", 0),
            verify_ssl=_bool("PLEX_VERIFY_SSL", True),
            mode=_clean_mode(_str("BRIDGE_MODE", "queue")),
            ungroup_on_play=_bool("UNGROUP_ON_PLAY", False),
            volume_limit=max(1, min(100, _int("VOLUME_LIMIT", 100))),
            include_zones=_list("INCLUDE_ZONES"),
            exclude_zones=_list("EXCLUDE_ZONES"),
            static_hosts=_list("SONOS_HOSTS"),
            discovery_interval=_float("DISCOVERY_INTERVAL", 60.0),
            discovery_mx=_int("DISCOVERY_MX", 2),
            discovery_attempts=_int("DISCOVERY_ATTEMPTS", 3),
            topology_interval=_float("TOPOLOGY_INTERVAL", 30.0),
            poll_interval=_float("POLL_INTERVAL", 5.0),
            sonos_sub_timeout=_int("SONOS_SUB_TIMEOUT", 600),
            http_timeout=_float("HTTP_TIMEOUT", 10.0),
            timeline_interval=_float("TIMELINE_INTERVAL", 1.0),
            update_check=_bool("UPDATE_CHECK", True),
            auto_update=_bool("AUTO_UPDATE", False),
            update_check_interval=_float("UPDATE_CHECK_INTERVAL", 21600.0),
            log_level=_str("LOG_LEVEL", "INFO").strip().upper(),
        )

    @classmethod
    def load(cls) -> Config:
        """Environment defaults with ``settings.json`` laid over the top."""
        config = cls.from_env()
        config.apply(config.read_settings())
        return config

    # -- persistence ----------------------------------------------------
    @property
    def settings_path(self) -> Path:
        return Path(self.config_dir) / SETTINGS_FILENAME

    def read_settings(self) -> dict[str, object]:
        path = self.settings_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOGGER.warning("Ignoring unreadable %s: %s", path, exc)
            return {}
        if not isinstance(data, dict):
            LOGGER.warning("Ignoring %s: expected a JSON object", path)
            return {}
        return {key: value for key, value in data.items() if key in EDITABLE}

    def write_settings(self, values: dict[str, object]) -> None:
        """Replace the stored overrides, atomically.

        A half-written settings file would be read as "no overrides" on the next
        start, silently reverting everything the user set, so the new contents
        land under a temporary name and are renamed over the old ones.
        """
        path = self.settings_path
        path.parent.mkdir(parents=True, exist_ok=True)
        stored = {key: value for key, value in values.items() if key in EDITABLE}
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(stored, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(path)

    def apply(self, values: dict[str, object]) -> None:
        """Coerce and apply *values*, recording which keys were overridden."""
        known = {f.name: f for f in fields(self)}
        for key, raw in values.items():
            if key not in EDITABLE or key not in known:
                continue
            try:
                setattr(self, key, _coerce(key, known[key].type, raw))
            except (TypeError, ValueError):
                LOGGER.warning("Ignoring unusable setting %s=%r", key, raw)
                continue
            self.overridden.add(key)

    # -- helpers --------------------------------------------------------
    def zone_allowed(self, zone_name: str) -> bool:
        """Apply the include/exclude filters (case-insensitive)."""
        name = zone_name.casefold()
        if self.include_zones and not any(
            name == zone.casefold() for zone in self.include_zones
        ):
            return False
        return not any(name == zone.casefold() for zone in self.exclude_zones)

    def machine_identifier(self, sonos_uid: str) -> str:
        """Deterministic Plex machine identifier for a Sonos player UID."""
        return str(uuid.uuid5(UUID_NAMESPACE, f"caldera-sonos:{sonos_uid}"))

    def player_name(self, zone_name: str) -> str:
        return f"{zone_name}{self.name_suffix}"


def _coerce(key: str, annotation: object, raw: object):
    """Turn a JSON or form value into the type the field declares."""
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    if key == "mode":
        return _clean_mode(str(raw))
    if key == "stream_format":
        return _clean_format(str(raw))
    if key == "log_level":
        level = str(raw).strip().upper()
        return level if level in ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO"
    if key == "volume_limit":
        return max(1, min(100, int(raw)))
    if key == "max_bitrate_kbps":
        return max(0, int(raw))
    if "list[str]" in text:
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        return [item.strip() for item in str(raw).split(",") if item.strip()]
    if "bool" in text:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if "int" in text:
        return int(raw)
    if "float" in text:
        return float(raw)
    return str(raw)


class SettingsStore:
    """Reads and writes the override file, and tells callers what changed.

    The settings page runs in the HTTP server's thread of control while the
    bridge is running loops of its own, so a save has to be a single, ordered
    operation - hence the lock - and has to report which keys moved, because
    some of them (the zone filters, the log level) mean work beyond assigning
    an attribute.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()

    def current(self) -> dict[str, object]:
        """Every editable key's effective value."""
        return {key: getattr(self.config, key) for key in EDITABLE}

    def save(self, updates: dict[str, object]) -> set[str]:
        """Apply and persist *updates*.  Returns the keys whose value changed."""
        with self._lock:
            before = dict(self.current())
            stored = self.config.read_settings()
            stored.update({k: v for k, v in updates.items() if k in EDITABLE})
            self.config.apply(stored)
            # Persist what the values became, not the strings a form sent: the
            # file is also something a person may open and edit.
            self.config.write_settings({key: getattr(self.config, key) for key in stored})
            after = self.current()
            return {key for key in EDITABLE if before[key] != after[key]}

    def reset(self, keys: list[str] | None = None) -> set[str]:
        """Drop overrides so the environment's values apply again."""
        with self._lock:
            before = dict(self.current())
            stored = self.config.read_settings()
            for key in list(stored) if keys is None else keys:
                stored.pop(key, None)
                self.config.overridden.discard(key)

            defaults = Config.from_env()
            for key in EDITABLE:
                if key not in stored:
                    setattr(self.config, key, getattr(defaults, key))
            self.config.apply(stored)
            self.config.write_settings({key: getattr(self.config, key) for key in stored})
            after = self.current()
            return {key for key in EDITABLE if before[key] != after[key]}

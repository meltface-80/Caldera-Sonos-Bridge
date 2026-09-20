"""Configuration: environment defaults, the settings file laid over them."""

from __future__ import annotations

import json

from calderabridge.config import Config, SettingsStore


def test_env_defaults(monkeypatch):
    monkeypatch.setenv("NAME_SUFFIX", " (Plex)")
    monkeypatch.setenv("BRIDGE_MODE", "direct")
    monkeypatch.setenv("EXCLUDE_ZONES", "Bathroom, Garage")
    monkeypatch.setenv("VOLUME_LIMIT", "70")

    config = Config.from_env()
    assert config.name_suffix == " (Plex)"
    assert config.mode == "direct"
    assert config.exclude_zones == ["Bathroom", "Garage"]
    assert config.volume_limit == 70
    assert config.settings_port == 32700


def test_settings_port_defaults_to_32700():
    assert Config().settings_port == 32700
    assert Config().player_port_base == 32600


def test_nonsense_values_fall_back(monkeypatch):
    monkeypatch.setenv("BRIDGE_MODE", "sideways")
    monkeypatch.setenv("HTTP_PORT", "not-a-port")
    monkeypatch.setenv("STREAM_FORMAT", "wav")

    config = Config.from_env()
    assert config.mode == "queue"
    assert config.settings_port == 32700
    assert config.stream_format == "original"


def test_zone_filters_are_case_insensitive():
    config = Config(include_zones=["kitchen"], exclude_zones=["Study"])
    assert config.zone_allowed("Kitchen")
    assert not config.zone_allowed("Study")
    assert not config.zone_allowed("Bedroom")


def test_machine_identifier_is_stable():
    config = Config()
    first = config.machine_identifier("RINCON_AAA01400")
    assert first == Config().machine_identifier("RINCON_AAA01400")
    assert first != config.machine_identifier("RINCON_BBB01400")


def test_settings_file_overlays_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NAME_SUFFIX", " (from env)")
    (tmp_path / "settings.json").write_text(
        json.dumps({"name_suffix": " (from the page)", "mode": "direct"})
    )
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))

    config = Config.load()
    assert config.name_suffix == " (from the page)"
    assert config.mode == "direct"
    assert "name_suffix" in config.overridden


def test_unknown_keys_in_the_settings_file_are_ignored(tmp_path, monkeypatch):
    (tmp_path / "settings.json").write_text(
        json.dumps({"settings_port": 9999, "name_suffix": " x"})
    )
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))

    config = Config.load()
    assert config.settings_port == 32700  # a port is never taken from the file
    assert config.name_suffix == " x"


def test_unreadable_settings_file_does_not_stop_startup(tmp_path, monkeypatch):
    (tmp_path / "settings.json").write_text("{ this is not json")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert Config.load().name_suffix == " (Sonos)"


def test_store_saves_and_reports_what_changed(tmp_path):
    config = Config(config_dir=str(tmp_path))
    store = SettingsStore(config)

    changed = store.save({"mode": "direct", "volume_limit": "60"})
    assert changed == {"mode", "volume_limit"}
    assert config.mode == "direct"
    assert config.volume_limit == 60

    # Saving the same values again changes nothing.
    assert store.save({"mode": "direct"}) == set()

    stored = json.loads((tmp_path / "settings.json").read_text())
    assert stored["mode"] == "direct"
    assert stored["volume_limit"] == 60


def test_store_coerces_form_strings(tmp_path):
    config = Config(config_dir=str(tmp_path))
    store = SettingsStore(config)

    store.save(
        {
            "include_zones": "Kitchen, Study",
            "ungroup_on_play": "on",
            "max_bitrate_kbps": "320",
        }
    )
    assert config.include_zones == ["Kitchen", "Study"]
    assert config.ungroup_on_play is True
    assert config.max_bitrate_kbps == 320


def test_store_clamps_out_of_range_values(tmp_path):
    config = Config(config_dir=str(tmp_path))
    store = SettingsStore(config)
    store.save({"volume_limit": "500", "max_bitrate_kbps": "-3"})
    assert config.volume_limit == 100
    assert config.max_bitrate_kbps == 0


def test_reset_restores_the_environment_value(tmp_path, monkeypatch):
    monkeypatch.setenv("NAME_SUFFIX", " (from env)")
    config = Config.from_env()
    config.config_dir = str(tmp_path)
    store = SettingsStore(config)

    store.save({"name_suffix": " (edited)"})
    assert config.name_suffix == " (edited)"

    changed = store.reset(["name_suffix"])
    assert changed == {"name_suffix"}
    assert config.name_suffix == " (from env)"
    assert "name_suffix" not in config.overridden
    assert json.loads((tmp_path / "settings.json").read_text()) == {}


def test_reset_everything(tmp_path):
    config = Config(config_dir=str(tmp_path))
    store = SettingsStore(config)
    store.save({"mode": "direct", "name_suffix": " x"})

    store.reset(None)
    assert config.mode == "queue"
    assert config.name_suffix == " (Sonos)"

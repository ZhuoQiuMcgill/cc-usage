"""Config persistence + validation (T0 §7). Hermetic via a tmp config path."""

import json

import cc_usage.config as c


def test_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "CONFIG_JSON", tmp_path / "config.json")
    cfg = c.Config(refresh_interval=10, default_window="5h", show_cost=False, theme="light")
    c.save_config(cfg)
    assert c.load_config() == cfg


def test_invalid_values_fall_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "CONFIG_JSON", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {"refresh_interval": 999, "default_window": "weird", "show_cost": "yes", "theme": "neon"}
        )
    )
    cfg = c.load_config()
    assert cfg.refresh_interval == 5
    assert cfg.default_window == "all"
    assert cfg.show_cost is True
    assert cfg.theme == "dark"


def test_missing_file_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "CONFIG_JSON", tmp_path / "nope.json")
    assert c.load_config() == c.Config()

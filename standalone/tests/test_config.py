"""rmbg_config：默认值、文件加载、坏配置报错。"""

import pytest
from standalone.model_names import DEFAULT_MODEL
from standalone.rmbg_config import (
    DEFAULT_PORT,
    Config,
    ConfigError,
)


def test_missing_file_gives_defaults(tmp_path):
    cfg = Config.load(tmp_path / "none.yaml")
    assert cfg.model is None
    assert cfg.resolve_model() == DEFAULT_MODEL
    assert cfg.host == "127.0.0.1"
    assert cfg.port == DEFAULT_PORT
    assert cfg.preload is True
    assert cfg.idle_kill_min == 5.0
    assert cfg.idle_unload_min == 5.0


def test_load_all_fields(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(
        "model: biref-lite\n"
        "host: 0.0.0.0\n"
        "port: 9000\n"
        "idle_unload_min: 2.5\n"
        "idle_kill_min: 0\n"
        "preload: false\n"
    )
    cfg = Config.load(f)
    assert cfg.model == "biref-lite"
    assert cfg.resolve_model() == "biref-lite"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000
    assert cfg.idle_unload_min == 2.5
    assert cfg.idle_kill_min == 0.0
    assert cfg.preload is False
    d = cfg.to_dict()
    assert d["model"] == "biref-lite" and d["port"] == 9000
    assert d["config_path"] == str(f)


def test_port_int_from_string(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("port: '8125'\n")
    assert Config.load(f).port == 8125


def test_bad_mapping(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("- a\n- b\n")
    with pytest.raises(ConfigError):
        Config.load(f)


def test_bad_port(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("port: not-a-number\n")
    with pytest.raises(ConfigError):
        Config.load(f)


def test_bad_idle_field(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("idle_kill_min: soon\n")
    with pytest.raises(ConfigError):
        Config.load(f)


def test_bad_preload(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("preload: maybe\n")
    with pytest.raises(ConfigError):
        Config.load(f)

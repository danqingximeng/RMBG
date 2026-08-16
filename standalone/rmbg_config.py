"""RMBG 配置加载。

优先级：CLI 参数 > config 文件 > 内建默认。默认配置文件在
~/.config/rmbg/config.yaml（`-c/--config` 可覆盖），不主动创建。

字段（全可选）：
    model            默认模型别名（缺省 inspyrenet）
    host             serve 绑定地址（默认 127.0.0.1）
    port             serve 端口（默认 8123）
    idle_unload_min  手动 daemon 空闲 N 分钟后卸载模型权重（默认 5，0=永不）
    idle_kill_min    托管 daemon 空闲 N 分钟后退出（默认 5，0=永不）
    preload          启动时预加载默认模型（默认 true）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from standalone.model_names import DEFAULT_MODEL

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "rmbg"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.yaml"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8123
DEFAULT_IDLE_UNLOAD_MIN = 5.0
DEFAULT_IDLE_KILL_MIN = 5.0
DEFAULT_PRELOAD = True


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    model: str | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    idle_unload_min: float = DEFAULT_IDLE_UNLOAD_MIN
    idle_kill_min: float = DEFAULT_IDLE_KILL_MIN
    preload: bool = DEFAULT_PRELOAD
    config_path: Path | None = None

    @classmethod
    def load(cls, config_path: Path | None = None) -> Config:
        if config_path is None:
            config_path = DEFAULT_CONFIG_FILE
        config_path = Path(config_path).expanduser()
        cfg = cls(config_path=config_path)
        if not config_path.exists():
            return cfg
        data = yaml.safe_load(config_path.read_text()) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"config {config_path} must be a YAML mapping")
        cfg.model = data.get("model")
        cfg.host = data.get("host", DEFAULT_HOST)
        try:
            cfg.port = int(data.get("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            raise ConfigError(f"invalid 'port' in config: {data.get('port')!r}")
        for key in ("idle_unload_min", "idle_kill_min"):
            raw = data.get(key)
            if raw is None:
                continue
            try:
                setattr(cfg, key, float(raw))
            except (TypeError, ValueError):
                raise ConfigError(f"invalid {key!r} in config: {raw!r}")
        preload = data.get("preload")
        if preload is not None:
            if not isinstance(preload, bool):
                raise ConfigError(f"invalid 'preload' in config: {preload!r}")
            cfg.preload = preload
        return cfg

    def resolve_model(self) -> str:
        return self.model or DEFAULT_MODEL

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "host": self.host,
            "port": self.port,
            "idle_unload_min": self.idle_unload_min,
            "idle_kill_min": self.idle_kill_min,
            "preload": self.preload,
            "config_path": str(self.config_path) if self.config_path else None,
        }

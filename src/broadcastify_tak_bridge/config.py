"""Configuration loading and validation."""

from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the service configuration is invalid."""


ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

DEFAULTS: dict[str, Any] = {
    "service": {
        "log_level": "INFO",
        "log_transcripts": False,
        "health_host": "127.0.0.1",
        "health_port": 8080,
    },
    "tak_server": {
        "port": 8089,
        "tls": True,
        "verify_hostname": True,
        "queue_size": 500,
        "reconnect_seconds": 5,
    },
    "broadcastify": {
        "username": "",
        "password": "",
        "channels": [{"source": "mono", "label": "DISPATCH"}],
        "chunk_seconds": 10,
        "reconnect_max_seconds": 120,
    },
    "whisper": {
        "model": "medium.en",
        "device": "cpu",
        "compute_type": "int8",
        "language": "en",
        "min_confidence": 0.35,
        "initial_prompt": (
            "Public safety radio dispatch. Engine, ladder, medic, police, "
            "fire, EMS, traffic collision, structure fire."
        ),
    },
    "tak_output": {
        "chat_room": "All Chat Rooms",
        "sender_uid": "broadcastify-bridge",
        "sender_callsign": "DISPATCH",
        "send_markers": True,
        "marker_stale_seconds": 1800,
    },
    "locations": {
        "file": "",
        "address_database": "",
        "geocoding": {
            "enabled": False,
            "region_suffix": "",
            "country_code": "",
            "user_agent": "broadcastify-tak-bridge/0.1",
        },
    },
}


def _merge(base: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _expand_environment(value: Any) -> Any:
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            missing.add(name)
            return ""
        return os.environ[name]

    def expand(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: expand(child) for key, child in item.items()}
        if isinstance(item, list):
            return [expand(child) for child in item]
        if isinstance(item, str):
            return ENV_PATTERN.sub(replace, item)
        return item

    expanded = expand(value)
    if missing:
        names = ", ".join(sorted(missing))
        raise ConfigError(f"Missing environment variables: {names}")
    return expanded


def _required(config: dict[str, Any], section: str, key: str) -> Any:
    value = config.get(section, {}).get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ConfigError(f"Missing required setting: {section}.{key}")
    return value


def validate_config(config: dict[str, Any]) -> None:
    _required(config, "tak_server", "host")
    _required(config, "broadcastify", "stream_url")

    tak = config["tak_server"]
    tak_port = tak.get("port")
    if not isinstance(tak_port, int) or not 1 <= tak_port <= 65535:
        raise ConfigError("tak_server.port must be between 1 and 65535")
    queue_size = tak.get("queue_size")
    if not isinstance(queue_size, int) or queue_size < 1:
        raise ConfigError("tak_server.queue_size must be greater than 0")
    if tak.get("tls", True):
        for key in ("cert", "key", "ca"):
            _required(config, "tak_server", key)

    channels = config["broadcastify"].get("channels")
    if not isinstance(channels, list) or not channels:
        raise ConfigError("broadcastify.channels must contain at least one channel")
    for index, channel in enumerate(channels):
        if not isinstance(channel, dict):
            raise ConfigError(f"broadcastify.channels[{index}] must be a mapping")
        source = channel.get("source")
        if source not in {"mono", "left", "right"}:
            raise ConfigError(
                f"broadcastify.channels[{index}].source must be mono, left, or right"
            )
        if not str(channel.get("label", "")).strip():
            raise ConfigError(f"broadcastify.channels[{index}].label is required")

    chunk_seconds = config["broadcastify"].get("chunk_seconds")
    if not isinstance(chunk_seconds, int) or not 1 <= chunk_seconds <= 300:
        raise ConfigError("broadcastify.chunk_seconds must be between 1 and 300")

    confidence = config["whisper"].get("min_confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ConfigError("whisper.min_confidence must be between 0 and 1")

    health_port = config["service"].get("health_port")
    if not isinstance(health_port, int) or not 0 <= health_port <= 65535:
        raise ConfigError("service.health_port must be between 0 and 65535")


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read configuration file {config_path}: {exc}") from exc

    try:
        values = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(values, dict):
        raise ConfigError("The configuration root must be a mapping")

    values = _expand_environment(values)
    config = _merge(DEFAULTS, values)
    validate_config(config)
    return config

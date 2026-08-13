"""Client configuration loaded from YAML with environment overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent
CLIENT_CONFIG_PATH = Path(
    os.environ.get("CCWGET_CLIENT_CONFIG", PROJECT_ROOT / "config_client.yaml")
)


def load_client_config() -> dict[str, str]:
    """Load client service settings and apply environment overrides.

    Returns:
        Mapping containing ``service_url`` and ``token`` values.
    """
    config: dict[str, Any] = {}
    if CLIENT_CONFIG_PATH.exists():
        with CLIENT_CONFIG_PATH.open("r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file) or {}
    service_url = os.environ.get(
        "CCWGET_SERVICE_URL", config.get("service_url", "http://127.0.0.1:4321")
    )
    token = os.environ.get("CCWGET_TOKEN", config.get("token", ""))
    return {"service_url": str(service_url).rstrip("/"), "token": str(token)}

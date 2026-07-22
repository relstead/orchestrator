"""Configuration management."""

import json
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = {
    "max_task_attempts": 3,
    "max_turns": 6,
    "execute_timeout": 30,
    "stale_claim_minutes": 30,
    "workers": [
        {
            "name": "default",
            "model": "auto",
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
        }
    ],
}


def load_config(vault_root: Path | None = None) -> dict[str, Any]:
    """Load config from vault/config.json or return defaults."""
    if vault_root is None:
        return DEFAULT_CONFIG.copy()
    
    config_path = vault_root / "config.json"
    if config_path.exists():
        try:
            user_config = json.loads(config_path.read_text())
            result = DEFAULT_CONFIG.copy()
            result.update(user_config)
            return result
        except Exception:
            return DEFAULT_CONFIG.copy()
    
    return DEFAULT_CONFIG.copy()


def save_config(vault_root: Path, config: dict[str, Any]) -> None:
    """Save config to vault/config.json."""
    config_path = vault_root / "config.json"
    config_path.write_text(json.dumps(config, indent=2))

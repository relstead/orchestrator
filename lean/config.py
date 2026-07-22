"""Configuration management with secret management support."""

import json
import os
import re
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = {
    "max_task_attempts": 3,
    "max_turns": 6,
    "execute_timeout": 30,
    "stale_claim_minutes": 30,
    "task_timeout_seconds": 300,  # 5 minutes default task timeout
    "workers": [
        {
            "name": "default",
            "model": "auto",
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
        }
    ],
}


def _resolve_env_vars(value: Any) -> Any:
    """
    Resolve environment variable references in config values.
    
    Supports ${VAR_NAME} and $VAR_NAME syntax.
    Example: "api_key": "${MY_API_KEY}" resolves to os.environ["MY_API_KEY"]
    """
    if isinstance(value, str):
        # Match ${VAR} or $VAR patterns
        pattern = r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)'
        
        def replace_env(match):
            var_name = match.group(1) or match.group(2)
            return os.environ.get(var_name, "")
        
        return re.sub(pattern, replace_env, value)
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


def load_config(vault_root: Path | None = None) -> dict[str, Any]:
    """
    Load config from vault/config.json or return defaults.
    
    Supports secret management via environment variables:
    - api_key: "${ENV_VAR_NAME}" resolves to os.environ["ENV_VAR_NAME"]
    - Any config value can use ${VAR} syntax
    """
    if vault_root is None:
        return _resolve_env_vars(DEFAULT_CONFIG.copy())
    
    config_path = vault_root / "config.json"
    if config_path.exists():
        try:
            user_config = json.loads(config_path.read_text())
            result = DEFAULT_CONFIG.copy()
            result.update(user_config)
            # Resolve env vars after merging
            return _resolve_env_vars(result)
        except Exception:
            return _resolve_env_vars(DEFAULT_CONFIG.copy())
    
    return _resolve_env_vars(DEFAULT_CONFIG.copy())


def save_config(vault_root: Path, config: dict[str, Any]) -> None:
    """
    Save config to vault/config.json.
    
    Note: Env var references are NOT saved - only resolved values.
    Use ${VAR} in config.json to reference env vars at load time.
    """
    config_path = vault_root / "config.json"
    config_path.write_text(json.dumps(config, indent=2))

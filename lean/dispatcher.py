"""
Action Dispatcher - executes actions in the sandbox.

Per §8.3, §8.7.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config
    from .vault import Vault
    from .sandbox import SpawnResult


@dataclass
class ActionResult:
    """Result of an action execution."""
    success: bool
    action_type: str
    output: str | None = None
    error: str | None = None


class AgentDispatcher:
    """
    Dispatches actions to sandbox or filesystem.
    
    Validates actions against tier (coding/plan) and vault containment.
    """
    
    def __init__(
        self,
        vault: Vault,
        config: Config,
        sandbox: Any | None = None,  # Sandbox instance, optional for read/write
    ):
        self.vault = vault
        self.config = config
        self.sandbox = sandbox
        self._changes: list[str] = []
    
    @property
    def changes(self) -> list[str]:
        """Get list of changed paths."""
        return self._changes.copy()
    
    def dispatch(self, action: dict, tier: str = "coding") -> ActionResult:
        """
        Dispatch an action.
        
        Args:
            action: Action dict with 'type' and action-specific fields
            tier: Task tier (coding/plan)
        
        Returns:
            ActionResult with outcome
        """
        action_type = action.get("type", "").lower()
        
        if action_type == "read":
            return self._dispatch_read(action)
        elif action_type == "write":
            return self._dispatch_write(action)
        elif action_type == "execute":
            return self._dispatch_execute(action, tier)
        elif action_type == "done":
            return ActionResult(success=True, action_type="done")
        elif action_type == "human_review":
            return ActionResult(success=True, action_type="human_review", output="Waiting for human input")
        else:
            return ActionResult(success=False, action_type=action_type, error=f"Unknown action type: {action_type}")
    
    def _dispatch_read(self, action: dict) -> ActionResult:
        """Read a file."""
        path = action.get("path")
        if not path:
            return ActionResult(success=False, action_type="read", error="No path specified")
        
        # Validate path is within vault
        if not self._validate_path(path):
            return ActionResult(success=False, action_type="read", error=f"Path outside vault: {path}")
        
        full_path = self.vault.vault_path / path
        if not full_path.exists():
            return ActionResult(success=False, action_type="read", error=f"File not found: {path}")
        
        try:
            content = full_path.read_text(encoding="utf-8")
            return ActionResult(success=True, action_type="read", output=content)
        except Exception as e:
            return ActionResult(success=False, action_type="read", error=str(e))
    
    def _dispatch_write(self, action: dict) -> ActionResult:
        """Write a file."""
        path = action.get("path")
        content = action.get("content", "")
        append = action.get("append", False)
        
        if not path:
            return ActionResult(success=False, action_type="write", error="No path specified")
        
        # Validate path is within vault
        if not self._validate_path(path):
            return ActionResult(success=False, action_type="write", error=f"Path outside vault: {path}")
        
        full_path = self.vault.vault_path / path
        
        # Ensure parent directory exists
        full_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            if append and full_path.exists():
                full_path.write_text(full_path.read_text(encoding="utf-8") + content, encoding="utf-8")
            else:
                full_path.write_text(content, encoding="utf-8")
            
            self._changes.append(path)
            return ActionResult(success=True, action_type="write", output=f"Wrote {len(content)} bytes to {path}")
        except Exception as e:
            return ActionResult(success=False, action_type="write", error=str(e))
    
    def _dispatch_execute(self, action: dict, tier: str) -> ActionResult:
        """Execute a command."""
        command = action.get("command")
        cwd = action.get("cwd", ".")
        timeout = action.get("timeout", 30)
        
        if not command:
            return ActionResult(success=False, action_type="execute", error="No command specified")
        
        # Use sandbox if available, otherwise skip for non-Windows
        if self.sandbox is None:
            return ActionResult(
                success=True,
                action_type="execute",
                output="(Sandbox not available - skipped)",
            )
        
        # Validate working directory
        if not self._validate_path(cwd):
            return ActionResult(success=False, action_type="execute", error=f"Working directory outside vault: {cwd}")
        
        # Execute in sandbox
        cwd_path = self.vault.vault_path / cwd
        
        try:
            result: SpawnResult = self.sandbox.spawn(
                command=command,
                cwd=str(cwd_path),
                timeout_seconds=timeout,
            )
            
            output = f"[exit {result.exit_code}]\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            
            return ActionResult(
                success=result.exit_code == 0,
                action_type="execute",
                output=output,
                error=None if result.exit_code == 0 else f"Exit code: {result.exit_code}",
            )
        except Exception as e:
            return ActionResult(success=False, action_type="execute", error=str(e))
    
    def _validate_path(self, path: str) -> bool:
        """Validate path is within vault."""
        from .sandbox import safe_vault_path
        
        vault_root = str(self.vault.vault_path)
        return safe_vault_path(path, vault_root) is not None
    
    def dispatch_actions(self, actions: list[dict], tier: str = "coding") -> list[ActionResult]:
        """Dispatch multiple actions, stopping on first failure."""
        results = []
        for action in actions:
            result = self.dispatch(action, tier)
            results.append(result)
            if not result.success and result.action_type in ("read", "write", "execute"):
                # Stop on critical failure
                break
        return results


def parse_action(action_data: dict | str) -> dict | None:
    """
    Parse action from various formats.
    
    Per §10: LLM outputs JSON array of actions.
    """
    if isinstance(action_data, str):
        try:
            action_data = json.loads(action_data)
        except json.JSONDecodeError:
            return None
    
    if not isinstance(action_data, dict):
        return None
    
    # Validate required fields
    action_type = action_data.get("type", "").lower()
    if not action_type:
        return None
    
    # Validate action-specific fields
    if action_type == "read":
        if "path" not in action_data:
            return None
    elif action_type == "write":
        if "path" not in action_data:
            return None
    elif action_type == "execute":
        if "command" not in action_data:
            return None
    elif action_type in ("done", "human_review"):
        pass
    else:
        return None
    
    return action_data


def extract_actions_from_text(text: str) -> list[dict]:
    """
    Extract actions from LLM response text.
    
    Handles:
    - JSON code blocks
    - Inline JSON
    - Markdown lists (fallback)
    """
    import re
    
    # Try JSON code block
    json_match = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", text)
    if json_match:
        try:
            actions = json.loads(json_match.group(1))
            return [a for a in (parse_action(a) for a in actions) if a]
        except json.JSONDecodeError:
            pass
    
    # Try inline JSON
    json_match = re.search(r"\[\s*\{[\s\S]*?\}\s*\]", text)
    if json_match:
        try:
            actions = json.loads(json_match.group(0))
            return [a for a in (parse_action(a) for a in actions) if a]
        except json.JSONDecodeError:
            pass
    
    return []

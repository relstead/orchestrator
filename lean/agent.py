"""
Agent module for Vault Orchestrator.

Handles action parsing, string-aware JSON extraction, and action dispatch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ActionType(str, Enum):
    """Available action types."""
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    WRITE_CONVENTION = "write_convention"  # Not implemented per §14
    ASK_HUMAN = "ask_human"
    PROPOSE_PLAN = "propose_plan"
    FINAL = "final"


@dataclass
class Action:
    """
    An action to be performed by the agent.
    
    Actions are parsed from LLM responses.
    """
    action: ActionType
    path: str | None = None
    content: str | None = None
    command: str | None = None
    timeout: int | None = None
    tasks: list[dict[str, Any]] | None = None
    message: str | None = None
    raw: str | None = None  # Original JSON for debugging


# String-aware JSON extraction
# Handles: braces inside strings, markdown code fences, prose before/after
JSON_EXTRACTION_RE = re.compile(
    r'```json\s*\n(.*?)\n```|'
    r'```\s*\n(.*?)\n```|'
    r'(\{[^}]*(?:"[^"]*"[^}]*)*\})',
    re.DOTALL
)


def extract_json(text: str) -> str | None:
    """
    Extract JSON from text that may contain markdown, prose, etc.
    
    String-aware: won't match on braces inside quoted strings.
    Handles:
    - Markdown code fences with JSON
    - Markdown code fences without language
    - Bare JSON object
    """
    matches = JSON_EXTRACTION_RE.findall(text)
    
    for match in matches:
        # Check each group (from the alternation)
        for group in match:
            if group and group.strip():
                candidate = group.strip()
                # Verify it looks like JSON object
                if candidate.startswith('{') and candidate.endswith('}'):
                    return candidate
    
    return None


def parse_action(response: str) -> Action | None:
    """
    Parse an action from an LLM response.
    
    The response may be:
    - Pure JSON: {"action": "...", ...}
    - Markdown with JSON block: ```json {"action": "..."} ```
    - Prose with JSON: "Here's my plan:\n```json {...}\n```"
    
    Returns None if no valid action found.
    """
    # Try to extract JSON
    json_str = extract_json(response)
    
    if not json_str:
        return None
    
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return None
    
    # Validate action type
    action_type_str = data.get("action", "")
    try:
        action_type = ActionType(action_type_str)
    except ValueError:
        return None
    
    # Build action
    action = Action(
        action=action_type,
        path=data.get("path"),
        content=data.get("content"),
        command=data.get("command"),
        timeout=data.get("timeout"),
        tasks=data.get("tasks"),
        message=data.get("message"),
        raw=json_str,
    )
    
    return action


def format_action_result(action: Action, success: bool, observation: str) -> str:
    """
    Format an action result for the transcript.
    
    Returns a formatted observation string.
    """
    result = f"[{'OK' if success else 'ERROR'}] {action.action.value}"
    
    if action.path:
        result += f" {action.path}"
    
    if observation:
        result += f"\n{observation}"
    
    return result


@dataclass
class TranscriptEntry:
    """A single entry in the task transcript."""
    turn: int
    role: str  # "agent" or "observation"
    content: str
    timestamp: float


@dataclass 
class Turn:
    """A single turn in the task execution."""
    turn_number: int
    observation: str | None = None
    action: Action | None = None
    action_success: bool | None = None
    action_result: str | None = None


@dataclass
class TaskTranscript:
    """
    In-task transcript per §9.3.
    
    Once an observation is superseded, the earlier copy collapses to a marker.
    """
    task_id: str
    entries: list[TranscriptEntry] = field(default_factory=list)
    superseded: dict[str, str] = field(default_factory=dict)  # path -> marker
    
    def add_turn(self, turn: Turn) -> None:
        """Add a turn to the transcript."""
        # Add observation if present
        if turn.observation:
            self.entries.append(TranscriptEntry(
                turn=turn.turn_number,
                role="observation",
                content=turn.observation,
                timestamp=0,  # Would be actual timestamp
            ))
        
        # Add action if present
        if turn.action:
            content = f"Action: {turn.action.action.value}"
            if turn.action.path:
                content += f" {turn.action.path}"
            if turn.action.command:
                content += f" {turn.action.command}"
            
            self.entries.append(TranscriptEntry(
                turn=turn.turn_number,
                role="agent",
                content=content,
                timestamp=0,
            ))
            
            # Add result
            if turn.action_result:
                self.entries.append(TranscriptEntry(
                    turn=turn.turn_number,
                    role="observation",
                    content=turn.action_result,
                    timestamp=0,
                ))
    
    def mark_superseded(self, path: str, turn: int) -> None:
        """Mark a path as superseded."""
        self.superseded[path] = f"[superseded: read {path} at turn {turn}]"
    
    def to_string(self) -> str:
        """Convert transcript to string for storage."""
        lines = []
        
        for entry in self.entries:
            if entry.content in self.superseded.values():
                continue  # Skip superseded entries
            
            role = entry.role.upper()
            lines.append(f"**{role}** (turn {entry.turn}):")
            lines.append(entry.content)
            lines.append("")
        
        return "\n".join(lines)
    
    def strip_for_done(self) -> str:
        """
        Strip transcript for done tasks.
        
        Per §9.3: working transcript stripped before filing to done/.
        """
        # Keep only the final result, not the process
        final_entries = []
        
        for entry in reversed(self.entries):
            if entry.role == "observation" and entry.content.startswith("[OK]"):
                final_entries.append(entry)
                break
        
        if not final_entries:
            return ""
        
        entry = final_entries[0]
        return f"[Result] {entry.content}"


# Action validation functions

def validate_write_path(path: str, project_dir: str, vault_root: str) -> bool:
    """
    Validate a write path is within the project.
    
    Returns True if allowed.
    """
    from pathlib import Path
    
    try:
        full_path = Path(project_dir) / path
        resolved = full_path.resolve()
        vault = Path(vault_root).resolve()
        resolved.relative_to(vault)
        return True
    except (ValueError, OSError):
        return False


def validate_read_path(path: str, project_dir: str) -> bool:
    """
    Validate a read path is within the project.
    
    Returns True if allowed.
    """
    from pathlib import Path
    
    try:
        full_path = Path(project_dir) / path
        resolved = full_path.resolve()
        project = Path(project_dir).resolve()
        resolved.relative_to(project)
        return True
    except (ValueError, OSError):
        return False


def validate_execute_command(command: str, tier: str, allowed_root: str) -> tuple[bool, str]:
    """
    Validate an execute command for the given tier.
    
    Returns (allowed, reason).
    """
    from . import sandbox
    
    # Check command-level allowlist/denylist
    allowed, reason = sandbox.is_command_allowed(command, tier)
    if not allowed:
        return False, reason
    
    # Check path containment in arguments
    # Extract paths from command
    parts = command.split()
    for part in parts:
        # Skip flags
        if part.startswith('-'):
            continue
        
        # Try to extract path
        if '/' in part or '\\' in part or ':' in part:
            result = sandbox.safe_vault_path(part, Path(allowed_root))
            if result is None:
                return False, f"Path outside allowed root: {part}"
    
    return True, "allowed"

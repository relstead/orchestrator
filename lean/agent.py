"""Minimal AI agent - inference only.

AI receives task + context, outputs JSON actions.
No exploration, no planning, no self-review.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path


SYSTEM_PROMPT = """You are a coding assistant. Complete tasks by taking actions.

Actions (respond with JSON):
{"reasoning": "why", "action": "read", "path": "file"}
{"reasoning": "why", "action": "write", "path": "file", "content": "..."}
{"reasoning": "why", "action": "execute", "command": "shell command"}
{"reasoning": "why", "action": "final", "result": "summary"}

Rules:
- Read before write (overwrites existing)
- Execute runs in project folder
- Call final when task complete"""


@dataclass
class Action:
    """An action from the AI."""
    reasoning: str
    action: str
    path: str | None = None
    content: str | None = None
    command: str | None = None
    result: str | None = None


def parse_action(text: str) -> Action | None:
    """Parse JSON action from AI response."""
    text = text.strip()
    
    if text.startswith("```"):
        text = re.sub(r"```[a-z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    
    start = text.find("{")
    if start == -1:
        return None
    
    depth = 0
    for i, c in enumerate(text[start:], start):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start:i+1])
                    return Action(
                        reasoning=data.get("reasoning", ""),
                        action=data.get("action", ""),
                        path=data.get("path"),
                        content=data.get("content"),
                        command=data.get("command"),
                        result=data.get("result"),
                    )
                except json.JSONDecodeError:
                    return None
    return None


def build_prompt(
    task_body: str,
    relevant_context: str,
    previous_failure: str | None = None,
) -> str:
    """Build prompt for AI."""
    parts = [f"# Task\n{task_body}\n"]
    
    if relevant_context:
        parts.append(f"\n# Context\n{relevant_context}\n")
    
    if previous_failure:
        parts.append(f"\n# Previous Attempt Failed\n{previous_failure}\n")
    
    parts.append(f"\n# System\n{SYSTEM_PROMPT}")
    
    return "\n".join(parts)

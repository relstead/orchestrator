"""Minimal AI agent - inference only.

AI receives task + context, outputs JSON actions.
No exploration, no planning, no self-review.

Canonical action taxonomy - single source of truth for both standalone and orchestrated modes.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path


# Canonical action taxonomy - single source of truth
# Both standalone and orchestrated modes use this prompt
SYSTEM_PROMPT = """You are a coding assistant. Complete tasks by taking actions.

Actions (respond with JSON):
{"reasoning": "why", "action": "read", "path": "file"}
{"reasoning": "why", "action": "write", "path": "file", "content": "..."}
{"reasoning": "why", "action": "apply_patch", "path": "file", "content": "old_text", "result": "new_text"}
{"reasoning": "why", "action": "apply_multi_patch", "path": "files", "content": "[{\"path\": \"f\", \"old_text\": \"...\", \"new_text\": \"...\"}]"}
{"reasoning": "why", "action": "execute", "command": "shell command"}
{"reasoning": "why", "action": "final", "result": "summary"}
{"reasoning": "why", "action": "list"}
{"reasoning": "why", "action": "ask_human", "path": "optional-id"}
{"reasoning": "why", "action": "find_references", "path": "symbol_name"}
{"reasoning": "why", "action": "find_definition", "path": "symbol_name"}
{"reasoning": "why", "action": "find_importers", "path": "module_name"}
{"reasoning": "why", "action": "find_tests", "path": "symbol_name"}
{"reasoning": "why", "action": "find_imports", "path": "file_path"}

Rules:
- Read before write (overwrites existing)
- apply_patch: use for targeted edits; must match old_text exactly
- apply_multi_patch: atomic multi-file patch; all must validate or all rejected
- Execute runs in project folder (opt-in, off by default)
- Call final when task complete
- find_references: find files that define or reference a symbol
- find_definition: find files that define a specific symbol
- find_importers: find files that import a specific module
- find_tests: find test files related to a symbol
- find_imports: show imports within a specific file"""


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
    """Parse JSON action from AI response.
    
    Uses a string-aware brace counter to handle content with braces inside
    string values (common in code, JSON, CSS, etc.).
    """
    text = text.strip()
    
    if text.startswith("```"):
        text = re.sub(r"```[a-z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    
    start = text.find("{")
    if start == -1:
        return None
    
    depth = 0
    in_string = False
    escape_next = False
    
    for i, c in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        
        if c == "\\":
            escape_next = True
            continue
        
        if c == '"' and not escape_next:
            in_string = not in_string
            continue
        
        # Only count braces outside of strings
        if not in_string:
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

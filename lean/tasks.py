"""
Task module for Vault Orchestrator.

Handles task file parsing, meta extraction, and task state management.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from .vault import Vault


class TaskType(str, Enum):
    """Task type enum."""
    CODING = "coding"
    PLAN = "plan"


class TaskState(str, Enum):
    """Task state enum (per §6)."""
    PENDING = "pending"
    DOING = "doing"
    DONE = "done"
    BLOCKED = "blocked"
    WAITING = "waiting"
    FAILED = "failed"


# Regex for parsing task meta
META_PATTERN = re.compile(
    r'<!--\s*meta:\s*'
    r'(?:type=(\w+)\s*)?'
    r'(?:attempts=(\d+)\s*)?'
    r'(?:depth=(\d+)\s*)?'
    r'(?:schema_version=(\d+)\s*)?'
    r'(?:depends-on=([\w,-]+)\s*)?'
    r'(?:origin-plan=(\S+)\s*)?'
    r'-->',
    re.IGNORECASE
)


@dataclass
class TaskMeta:
    """Task metadata parsed from the meta comment."""
    type: TaskType = TaskType.CODING
    attempts: int = 0
    depth: int = 0
    schema_version: int = 1
    depends_on: list[str] = field(default_factory=list)
    origin_plan: str | None = None
    
    @classmethod
    def parse(cls, content: str) -> "TaskMeta":
        """Parse meta from task file content."""
        match = META_PATTERN.search(content)
        if not match:
            return cls()
        
        groups = match.groups()
        
        task_type = TaskType.CODING
        if groups[0]:
            try:
                task_type = TaskType(groups[0].lower())
            except ValueError:
                pass
        
        attempts = int(groups[1]) if groups[1] else 0
        depth = int(groups[2]) if groups[2] else 0
        schema_version = int(groups[3]) if groups[3] else 1
        
        depends_on = []
        if groups[4]:
            depends_on = [d.strip() for d in groups[4].split(',') if d.strip()]
        
        return cls(
            type=task_type,
            attempts=attempts,
            depth=depth,
            schema_version=schema_version,
            depends_on=depends_on,
            origin_plan=groups[5],
        )
    
    def to_comment(self) -> str:
        """Convert back to meta comment format."""
        parts = ["<!-- meta:"]
        parts.append(f"type={self.type.value}")
        parts.append(f"attempts={self.attempts}")
        parts.append(f"depth={self.depth}")
        parts.append(f"schema_version={self.schema_version}")
        if self.depends_on:
            parts.append(f"depends-on={','.join(self.depends_on)}")
        if self.origin_plan:
            parts.append(f"origin-plan={self.origin_plan}")
        parts.append("-->")
        return " ".join(parts)


@dataclass
class Task:
    """
    A task loaded from a task file.
    
    Task File Schema (§5):
        <!-- meta: type=coding attempts=0 depth=0 schema_version=1 depends-on=... -->
        # Title
        
        Body text...
    """
    id: str  # Filename without extension
    path: Path
    meta: TaskMeta
    title: str
    body: str
    raw_content: str
    
    @property
    def state(self) -> TaskState:
        """Get the task state from its directory."""
        parent = self.path.parent.name
        try:
            return TaskState(parent)
        except ValueError:
            return TaskState.PENDING
    
    @property
    def project(self) -> str | None:
        """Get the project name from the path."""
        # Path: Projects/<project>/tasks/<state>/<id>.md
        parts = self.path.parts
        try:
            projects_idx = parts.index("Projects")
            if projects_idx + 1 < len(parts):
                return parts[projects_idx + 1]
        except ValueError:
            pass
        return None
    
    @classmethod
    def from_file(cls, path: Path) -> "Task":
        """Load a task from a file."""
        content = path.read_text()
        meta = TaskMeta.parse(content)
        
        # Extract title (first # heading)
        title = ""
        body_lines = []
        in_body = False
        
        for line in content.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                in_body = True
            elif in_body and not line.startswith("<!--"):
                body_lines.append(line)
        
        body = "\n".join(body_lines).strip()
        
        # Task ID is filename without extension
        task_id = path.stem
        
        return cls(
            id=task_id,
            path=path,
            meta=meta,
            title=title,
            body=body,
            raw_content=content,
        )
    
    def to_content(self, include_transcript: bool = True) -> str:
        """Convert task back to file content."""
        lines = [self.meta.to_comment()]
        if self.title:
            lines.append(f"# {self.title}")
        lines.append("")
        lines.append(self.body)
        
        if include_transcript and "## Transcript" in self.raw_content:
            # Keep transcript if it was in the original
            idx = self.raw_content.find("## Transcript")
            lines.append("")
            lines.append(self.raw_content[idx:])
        
        return "\n".join(lines)
    
    def increment_attempts(self) -> None:
        """Increment the attempt counter."""
        self.meta.attempts += 1
    
    def set_depends_on(self, task_ids: list[str]) -> None:
        """Set the depends-on list."""
        self.meta.depends_on = task_ids


class TaskStore:
    """
    Manages task files in a vault.
    
    Provides methods for creating, reading, updating, and moving tasks.
    """
    
    def __init__(self, vault: "Vault"):
        self._vault = vault
    
    def get_tasks_in_state(self, project: str, state: TaskState) -> list[Task]:
        """Get all tasks in a given state for a project."""
        tasks_dir = self._vault.get_project_path(project) / "tasks" / state.value
        if not tasks_dir.exists():
            return []
        
        tasks = []
        for f in tasks_dir.glob("*.md"):
            try:
                tasks.append(Task.from_file(f))
            except Exception:
                pass
        
        return sorted(tasks, key=lambda t: t.id)
    
    def get_task(self, project: str, task_id: str) -> Task | None:
        """Get a specific task by ID (searches all states)."""
        project_path = self._vault.get_project_path(project)
        
        for state in TaskState:
            task_path = project_path / "tasks" / state.value / f"{task_id}.md"
            if task_path.exists():
                try:
                    return Task.from_file(task_path)
                except Exception:
                    pass
        
        return None
    
    def move_task(self, task: Task, new_state: TaskState) -> Task:
        """
        Move a task to a new state.
        
        This is the state transition per §6.
        """
        # Compute new path
        project = task.project
        if not project:
            raise ValueError("Cannot determine project for task")
        
        new_dir = self._vault.get_project_path(project) / "tasks" / new_state.value
        new_path = new_dir / f"{task.id}.md"
        
        # Read current content
        content = task.to_content()
        
        # Write to new location
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(content)
        
        # Delete old file
        if task.path.exists() and task.path != new_path:
            task.path.unlink()
        
        # Reload task from new location
        return Task.from_file(new_path)
    
    def create_task(
        self,
        project: str,
        title: str,
        body: str,
        task_type: TaskType = TaskType.CODING,
        depends_on: list[str] | None = None,
        origin_plan: str | None = None,
    ) -> Task:
        """
        Create a new task.
        
        Generates a unique ID and creates the task file in pending/.
        """
        project_path = self._vault.ensure_project_skeleton(project)
        pending_dir = project_path / "tasks" / "pending"
        
        # Generate unique ID
        existing_ids = set()
        if pending_dir.exists():
            for f in pending_dir.glob("*.md"):
                existing_ids.add(f.stem)
        
        # Find next available ID
        counter = 1
        while f"task-{counter:02d}" in existing_ids:
            counter += 1
        
        task_id = f"task-{counter:02d}"
        
        # Build meta
        meta = TaskMeta(
            type=task_type,
            attempts=0,
            depth=0,
            schema_version=1,
            depends_on=depends_on or [],
            origin_plan=origin_plan,
        )
        
        # Build content
        lines = [
            meta.to_comment(),
            f"# {title}",
            "",
            body,
        ]
        content = "\n".join(lines)
        
        # Write file
        task_path = pending_dir / f"{task_id}.md"
        task_path.write_text(content)
        
        return Task.from_file(task_path)
    
    def delete_task(self, task: Task) -> None:
        """Delete a task file."""
        if task.path.exists():
            task.path.unlink()
    
    def update_task(self, task: Task, include_transcript: bool = True) -> None:
        """Update the task file with current task state."""
        content = task.to_content(include_transcript=include_transcript)
        task.path.write_text(content)

"""
Logger and Metrics module for Vault Orchestrator.

Handles event logging to events.jsonl and file compaction.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class TaskEvent:
    """A single task attempt event for metrics."""
    ts: str  # ISO timestamp
    task_id: str
    type: str  # "coding" or "plan"
    turns_used: int
    max_turns: int
    outcome: str  # "done", "failed", "blocked"
    files_touched: int = 0
    files_created: int = 0
    files_overwritten: int = 0
    changeset_from_attempt: int = 0
    context_files_offered_indexer: int = 0
    context_files_offered_planner: int = 0
    context_files_read_indexer: int = 0
    context_files_read_planner: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    plan_depth: int = 0
    retry_of: str | None = None
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {k: v for k, v in asdict(self).items() if v is not None}
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskEvent":
        """Create from dictionary."""
        return cls(**data)
    
    @classmethod
    def create(
        cls,
        task_id: str,
        task_type: str,
        turns_used: int,
        max_turns: int,
        outcome: str,
        plan_depth: int = 0,
        **kwargs,
    ) -> "TaskEvent":
        """Create a new event with current timestamp."""
        return cls(
            ts=datetime.utcnow().isoformat() + "Z",
            task_id=task_id,
            type=task_type,
            turns_used=turns_used,
            max_turns=max_turns,
            outcome=outcome,
            plan_depth=plan_depth,
            **kwargs,
        )


class MetricsWriter:
    """
    Append-only metrics writer.
    
    Writes events to events.jsonl and handles compaction.
    Per §9.7 and §11.
    """
    
    def __init__(self, vault_root: Path):
        self._vault_root = vault_root
        self._events_path = vault_root / "_metrics" / "events.jsonl"
        self._archive_path = vault_root / "_archive" / "_metrics"
        self._compact_at_bytes = 1024 * 1024  # 1MB, configurable
    
    @property
    def events_path(self) -> Path:
        return self._events_path
    
    def append(self, event: TaskEvent) -> None:
        """
        Append an event to the metrics log.
        
        Events are appended one per line in JSONL format.
        This is append-only - never read-modify-rewrite.
        """
        self._events_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(self._events_path, "a") as f:
            f.write(json.dumps(event.to_dict()) + "\n")
    
    def should_compact(self) -> bool:
        """Check if compaction should run."""
        if not self._events_path.exists():
            return False
        return self._events_path.stat().st_size >= self._compact_at_bytes
    
    def compact(self) -> int:
        """
        Compact events into the archive.
        
        Moves all current events to archive and starts a new file.
        Returns the number of events archived.
        """
        if not self._events_path.exists():
            return 0
        
        # Read all events
        events = []
        with open(self._events_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        
        if not events:
            return 0
        
        # Archive to timestamped file (preserves all data)
        self._archive_path.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time())
        archive_file = self._archive_path / f"events_{timestamp}.jsonl"
        
        with open(archive_file, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")
        
        # Clear the live file (keep for new events)
        self._events_path.write_text("")
        
        return len(events)
    
    def read_events(self) -> list[TaskEvent]:
        """Read all events from the live file and archive."""
        events = []
        
        # Read live file
        if self._events_path.exists():
            with open(self._events_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(TaskEvent.from_dict(json.loads(line)))
        
        # Read archived files
        if self._archive_path.exists():
            for archive_file in sorted(self._archive_path.glob("events_*.jsonl")):
                with open(archive_file) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            events.append(TaskEvent.from_dict(json.loads(line)))
        
        return events


class DigestWriter:
    """
    Append-first digest writer with compaction.
    
    Writes digest entries and handles compaction at size threshold.
    Per §9.4.
    """
    
    def __init__(self, digest_path: Path, archive_path: Path):
        self._digest_path = digest_path
        self._archive_path = archive_path
        self._compact_at_bytes = 1024 * 1024  # 1MB
        self._cooldown_until: float = 0  # Timestamp for backoff
    
    def append(self, line: str) -> bool:
        """
        Append a line to the digest.
        
        Returns True if successful, False if skipped (backoff or compaction).
        """
        # Check backoff
        if time.time() < self._cooldown_until:
            return False
        
        self._digest_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(self._digest_path, "a") as f:
            f.write(line + "\n")
        
        # Check if compaction needed
        if self.should_compact():
            self.compact()
        
        return True
    
    def should_compact(self) -> bool:
        """Check if compaction should run."""
        if not self._digest_path.exists():
            return False
        return self._digest_path.stat().st_size >= self._compact_at_bytes
    
    def compact(self) -> bool:
        """
        Compact the digest.
        
        Archives the current digest and starts fresh.
        Returns True if compaction ran, False if skipped.
        """
        if not self._digest_path.exists():
            return False
        
        content = self._digest_path.read_text()
        if not content.strip():
            return False
        
        # Archive byte-identical (per §9.4)
        self._archive_path.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time())
        archive_file = self._archive_path / f"digest_{timestamp}.md"
        archive_file.write_text(content)
        
        # Keep just the header (first line) in live file
        lines = content.splitlines()
        if lines:
            self._digest_path.write_text(lines[0] + "\n")
        
        # Set cooldown to prevent immediate retry
        self._cooldown_until = time.time() + 60  # 1 minute backoff
        
        return True
    
    def set_backoff(self, seconds: float = 60) -> None:
        """Set backoff period."""
        self._cooldown_until = time.time() + seconds


def format_digest_line(entry_type: str, **kwargs) -> str:
    """
    Format a digest line.
    
    Format: [timestamp] type: message
    """
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"[{ts}] {entry_type}:"]
    for key, value in kwargs.items():
        parts.append(f" {key}={value}")
    return "".join(parts)

"""Main orchestrator - DAG + Worker Pool + Task Dispatch.

Code handles: dependency resolution, worker allocation, context building,
verification, rollback. AI handles: inference only.
"""

import json
import os
import re
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .agent import Action, parse_action, SYSTEM_PROMPT
from .config import load_config
from .dependency import DependencyGraph, build_graph_from_paths
from .indexer import Indexer
from .logger import Logger, LogLevel, get_default_logger
from .rollback import RollbackManager
from .sandbox import execute
from .tasks import (
    parse_task, claim_task, release_task, finish_task,
    scan_pending_tasks, scan_doing_tasks, is_stale, extract_dependencies,
)
from .verification import verify
from .vault import ensure_vault_skeleton, safe_vault_path, safe_project_path, list_project_dirs, ensure_project_skeleton, new_task_file
from .worker import WorkerPool


# Default task timeout (seconds)
DEFAULT_TASK_TIMEOUT_SECONDS = 300  # 5 minutes


class KnowledgeProvider:
    """Facade for knowledge/index operations.
    
    Wraps Indexer + ObservationCache to provide a clean interface for
    knowledge operations. In-process, no RPC, no new thread.
    """
    
    def __init__(self, indexer: Indexer):
        self._indexer = indexer
        self._cache = ObservationCache(indexer)
    
    def clear_cache(self) -> None:
        """Clear the observation cache."""
        self._cache.clear()
    
    def get_cache_stats(self) -> dict:
        """Get cache statistics."""
        return self._cache.get_stats()
    
    def find_references(self, symbol: str) -> list:
        """Find all files that define or reference a symbol."""
        return self._cache.find_references(symbol)
    
    def find_definition(self, symbol: str) -> list:
        """Find files that define a specific symbol."""
        return self._cache.find_definition(symbol)
    
    def find_importers(self, module: str) -> list:
        """Find files that import a specific module."""
        return self._cache.find_importers(module)
    
    def project_summary(self) -> dict:
        """Get a summary of the indexed project."""
        return {
            "files": len(self._indexer.files),
            "symbols": len(self._indexer._symbol_index),
            "languages": list(set(f.language for f in self._indexer.files.values() if f.language)),
        }


class ObservationCache:
    """Per-attempt cache for deterministic index lookups.
    
    Caches results of find_references, find_definition, find_importers
    and invalidates entries when their dependent files are modified.
    """
    
    def __init__(self, indexer: Indexer):
        self._indexer = indexer
        # Cache: (method_name, query) -> (result, dependent_files)
        self._cache: dict[tuple[str, str], tuple[list, frozenset]] = {}
    
    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()
    
    def find_references(self, symbol: str) -> list:
        """Find references with caching."""
        key = ("find_references", symbol)
        
        if key in self._cache:
            result, _ = self._cache[key]
            return result
        
        # Call the indexer and cache
        result = self._indexer.find_references(symbol)
        
        # Track which files this result depends on
        dependent_files = frozenset(entry.path for entry in result)
        self._cache[key] = (result, dependent_files)
        return result
    
    def find_definition(self, symbol: str) -> list:
        """Find definition with caching."""
        key = ("find_definition", symbol)
        
        if key in self._cache:
            result, _ = self._cache[key]
            return result
        
        # Call the indexer and cache
        result = self._indexer.find_definition(symbol)
        
        # Track which files this result depends on
        dependent_files = frozenset(entry.path for entry in result)
        self._cache[key] = (result, dependent_files)
        return result
    
    def find_importers(self, module: str) -> list:
        """Find importers with caching."""
        key = ("find_importers", module)
        
        if key in self._cache:
            result, _ = self._cache[key]
            return result
        
        # Call the indexer and cache
        result = self._indexer.find_importers(module)
        
        # Track which files this result depends on
        dependent_files = frozenset(entry.path for entry in result)
        self._cache[key] = (result, dependent_files)
        return result
    
    def invalidate_file(self, file_path: str) -> None:
        """Invalidate cache entries that depend on the given file.
        
        Any cached entry that references the given file path is dropped.
        """
        path_to_remove = file_path if not file_path.startswith('/') else file_path
        
        # Find and remove cache entries that depend on this file
        keys_to_remove = []
        for (method, query), (result, dependent_files) in self._cache.items():
            if path_to_remove in dependent_files:
                keys_to_remove.append((method, query))
        
        for key in keys_to_remove:
            del self._cache[key]
    
    def get_stats(self) -> dict:
        """Get cache statistics for debugging."""
        return {
            "entries": len(self._cache),
            "methods": len(set(k[0] for k in self._cache.keys())),
        }


@dataclass
class ExecutionResult:
    """Result of task execution."""
    success: bool
    output: str
    changes: list[str]
    cost_usd: float = 0.0
    duration_seconds: float = 0.0


class AuditLog:
    """
    Tracks who/when/what events in _audit.md.
    Per SPEC.md: vault files are the source of truth.
    """
    
    def __init__(self, vault_root: Path):
        self.audit_path = vault_root / "_audit.md"
        self._lock = threading.Lock()
    
    def log(self, event_type: str, detail: str, **extra: Any) -> None:
        """Append an audit entry."""
        timestamp = datetime.now().isoformat(timespec="seconds")
        
        entry_parts = [f"[{timestamp}] {event_type}: {detail}"]
        for key, value in extra.items():
            if value is not None:
                entry_parts.append(f"  {key}={value}")
        
        entry = " | ".join(entry_parts) + "\n"
        
        with self._lock:
            self.audit_path.write_text(
                self.audit_path.read_text() + entry,
                encoding="utf-8",
            )


class CostTracker:
    """
    Tracks cost per project and per model.
    Persists to _costs.json in vault root.
    """
    
    def __init__(self, vault_root: Path):
        self.costs_path = vault_root / "_costs.json"
        self._lock = threading.Lock()
        self._costs = self._load()
    
    def _load(self) -> dict[str, Any]:
        """Load costs from disk."""
        if self.costs_path.exists():
            try:
                return json.loads(self.costs_path.read_text())
            except Exception:
                pass
        return {"total_usd": 0.0, "by_project": {}, "by_model": {}}
    
    def _save(self) -> None:
        """Save costs to disk."""
        self.costs_path.write_text(json.dumps(self._costs, indent=2), encoding="utf-8")
    
    def record(self, project: str, model: str, cost_usd: float) -> None:
        """Record a cost entry."""
        if cost_usd <= 0:
            return
        
        with self._lock:
            self._costs["total_usd"] = self._costs.get("total_usd", 0.0) + cost_usd
            self._costs["by_project"][project] = self._costs.get("by_project", {}).get(project, 0.0) + cost_usd
            self._costs["by_model"][model] = self._costs.get("by_model", {}).get(model, 0.0) + cost_usd
            self._save()
    
    def get_summary(self) -> dict[str, Any]:
        """Get cost summary."""
        return self._costs.copy()


# Prompt templates per task type (imported from agent.py for consistency)
DEFAULT_PROMPT_TEMPLATES = {
    "general": SYSTEM_PROMPT,
    "coding": SYSTEM_PROMPT,  # Same prompt - find_* actions already included
    "reasoning": SYSTEM_PROMPT,
}


# Changeset Handoff Manifest - tracks files changed across task attempts
# This prevents workers from re-deriving state that was already modified
_CHANGESET_LINE_RE = re.compile(r"^- (.+?) \((created|overwritten)\)$", re.MULTILINE)


def parse_changeset_from_log(prior_log: str) -> dict[str, str]:
    """
    Parse the changeset manifest from a prior attempt's log.
    
    This is used to seed the changeset for the current attempt,
    ensuring changes accumulate across all attempts in a task's lifetime.
    """
    if not prior_log:
        return {}
    
    changed = {}
    for path, status in _CHANGESET_LINE_RE.findall(prior_log):
        changed[path] = status  # later occurrences win
    return changed


def format_changeset(changed_paths: dict[str, str]) -> str:
    """
    Format the changeset manifest for inclusion in prompts.
    
    This is inserted at the top of the prompt so the next worker
    knows exactly what files were modified - no need to re-scan.
    """
    if not changed_paths:
        return ""
    
    lines = [f"- {p} ({status})" for p, status in changed_paths.items()]
    return (
        "\n## Files Changed So Far (Across All Attempts)\n"
        "If picking up from a prior attempt, verify ONLY these specific files.\n"
        "No need to broadly re-scan the project:\n" + "\n".join(lines) + "\n"
    )


def format_changeset_summary(changed_paths: dict[str, str]) -> str:
    """Compact one-line summary for human-facing output."""
    if not changed_paths:
        return ""
    files = list(changed_paths.keys())
    if len(files) == 1:
        return f"**Files:** {files[0]}"
    elif len(files) <= 3:
        return f"**Files:** {', '.join(files)}"
    else:
        return f"**Files:** {files[0]} +{len(files)-1} more"


class Orchestrator:
    """
    Main orchestrator coordinating tasks and workers.
    
    - Builds DAG from task dependencies
    - Assigns ready tasks to workers (smart routing by task_type)
    - Runs verification
    - Handles rollback on failure
    - Enforces task timeouts
    - Tracks costs and audit log
    - Supports hot reload config
    - Uses structured JSON logging
    """
    
    def __init__(
        self,
        vault_root: Path,
        log_fn: Callable[[str], None] | None = None,
        logger: Logger | None = None,
    ):
        self.vault_root = vault_root
        self._logger = logger or get_default_logger(vault_root)
        self.log = log_fn or (lambda x: self._logger.info(x))
        
        # Config with hot reload support
        self._config_mtime: float = 0
        self._load_config()
        
        self.rollback = RollbackManager(vault_root)
        
        # Workers
        self.pool = WorkerPool(self.config.get("workers", []))
        
        # Audit log
        self.audit = AuditLog(vault_root)
        
        # Cost tracking
        self.costs = CostTracker(vault_root)
        
        # Prompt templates
        self.prompt_templates = self.config.get("prompt_templates", DEFAULT_PROMPT_TEMPLATES)
        
        # Instance ID for claiming tasks
        self.instance_id = f"{os.environ.get('COMPUTERNAME', os.environ.get('HOSTNAME', 'host'))}-{os.getpid()}"
        
        # Project index cache
        self._index_cache: dict[str, Indexer] = {}
        
        # Per-attempt knowledge provider (per project) - wraps Indexer + ObservationCache
        self._knowledge_providers: dict[str, KnowledgeProvider] = {}
        
        # Control
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._pause.set()  # Not paused by default
        self._thread: threading.Thread | None = None
        
        # Task timeout
        self._task_timeout = self.config.get("task_timeout_seconds", DEFAULT_TASK_TIMEOUT_SECONDS)
        
        # Execute is opt-in, off by default
        self._allow_execute = self.config.get("allow_execute", False)
        
        # Ensure vault structure
        ensure_vault_skeleton(vault_root)
        
        self._logger.info("Orchestrator initialized", vault=str(vault_root))
    
    def _load_config(self) -> None:
        """Load config, tracking mtime for hot reload."""
        self.config = load_config(self.vault_root)
        config_path = self.vault_root / "config.json"
        if config_path.exists():
            self._config_mtime = config_path.stat().st_mtime
        else:
            self._config_mtime = 0
    
    def _check_config_reload(self) -> bool:
        """Check if config has changed and reload if needed. Returns True if reloaded."""
        config_path = self.vault_root / "config.json"
        if not config_path.exists():
            return False
        
        current_mtime = config_path.stat().st_mtime
        if current_mtime != self._config_mtime:
            self._logger.info("Config file changed, reloading", 
                            old_mtime=self._config_mtime, new_mtime=current_mtime)
            self._load_config()
            
            # Reload workers in place (per SPEC.md: settings update in place)
            self.pool = WorkerPool(self.config.get("workers", []))
            
            # Reload prompt templates
            self.prompt_templates = self.config.get("prompt_templates", DEFAULT_PROMPT_TEMPLATES)
            
            # Reload task timeout
            self._task_timeout = self.config.get("task_timeout_seconds", DEFAULT_TASK_TIMEOUT_SECONDS)
            
            # Reload execute flag
            self._allow_execute = self.config.get("allow_execute", False)
            
            self.audit.log("config_reload", "Configuration reloaded from file")
            self._logger.info("Config reloaded successfully")
            return True
        return False
    
    def pause(self) -> None:
        """Pause task processing."""
        self._pause.clear()
        self._logger.info("Orchestrator paused")
        self.audit.log("pause", "Task processing paused")
    
    def resume(self) -> None:
        """Resume task processing."""
        self._pause.set()
        self._logger.info("Orchestrator resumed")
        self.audit.log("resume", "Task processing resumed")
    
    def is_paused(self) -> bool:
        """Check if orchestrator is paused."""
        return not self._pause.is_set()
    
    # =========================================================================
    # Project Indexing
    # =========================================================================
    
    def _get_indexer(self, project_path: Path) -> Indexer:
        """Get or create indexer for project."""
        key = str(project_path)
        if key not in self._index_cache:
            self._index_cache[key] = Indexer(project_path)
            self._index_cache[key].build()
        return self._index_cache[key]
    
    def _get_relevant_context(self, project_path: Path, task_body: str) -> str:
        """Get relevant file context for a task."""
        indexer = self._get_indexer(project_path)
        relevant = indexer.find_relevant(
            query=task_body,
            top_n=5,
            prefer_languages=["python", "javascript", "typescript", "go", "rust"],
        )
        return indexer.get_context(relevant, max_chars=6000)
    
    def _get_prompt(self, task_type: str, task_body: str, context: str) -> str:
        """
        Build a prompt using the appropriate template for the task type.
        
        Falls back to 'general' template if task_type not found.
        """
        template = self.prompt_templates.get(task_type, self.prompt_templates.get("general", ""))
        
        # Build full prompt with context
        if context:
            return f"""{template}

## Task
{task_body}

## Relevant Context
{context}
"""
        return f"""{template}

## Task
{task_body}
"""
    
    # =========================================================================
    # Crash Recovery
    # =========================================================================
    
    def _sweep_stale_claims(self, max_age_minutes: int = 30) -> int:
        """Recover stale task claims."""
        recovered = 0
        doing_tasks = scan_doing_tasks(self.vault_root)
        
        for task_path in doing_tasks:
            if is_stale(task_path, self.instance_id, max_age_minutes):
                self.log(f"Recovering stale task: {task_path.name}")
                release_task(task_path, back_to_pending=True)
                recovered += 1
        
        return recovered
    
    # =========================================================================
    # Inbox Processing
    # =========================================================================
    
    def _process_inbox(self) -> int:
        """
        Process _inbox.md entries and convert them to tasks.
        
        Inbox format supports inline project targeting:
        - "Fix the bug" → uses active project (from _active.md or default)
        - "[project-name] Fix the bug" → uses specified project
        - "@project-name: Fix the bug" → uses specified project (alt syntax)
        
        Scans inbox for new lines, creates task files, archives processed content.
        Returns number of tasks created.
        """
        inbox_path = self.vault_root / "_inbox.md"
        archive_path = self.vault_root / "_inbox_archive.md"
        
        if not inbox_path.exists():
            return 0
        
        inbox_content = inbox_path.read_text(encoding="utf-8")
        if not inbox_content.strip():
            return 0
        
        # Get active project (default to "default")
        active_project = self._get_active_project()
        
        # Parse inbox lines (non-empty, non-header lines)
        lines = inbox_content.split("\n")
        new_lines = []
        tasks_created = 0
        
        for line in lines:
            stripped = line.strip()
            # Skip empty lines and markdown headers
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            
            # Parse inline project targeting: "[project] task" or "@project: task"
            project = active_project
            task_body = stripped
            
            # Check for [project] syntax
            import re
            bracket_match = re.match(r'^\[([^\]]+)\]\s*(.+)$', stripped)
            if bracket_match:
                project = bracket_match.group(1).strip()
                task_body = bracket_match.group(2).strip()
            else:
                # Check for @project: syntax
                at_match = re.match(r'^@([^\s:]+):\s*(.+)$', stripped)
                if at_match:
                    project = at_match.group(1).strip()
                    task_body = at_match.group(2).strip()
            
            # Skip lines that are too short or too long
            if len(task_body) < 10 or len(task_body) > 500:
                new_lines.append(line)
                continue
            
            # This is a task entry - create it
            try:
                project_path = self.vault_root / "Projects" / project
                
                # Ensure project exists (auto-create if needed)
                if not project_path.exists():
                    ensure_project_skeleton(self.vault_root, project)
                    self._update_digest(f"Auto-created project: {project}", "info")
                
                task_path = new_task_file(project_path, "general", task_body)
                tasks_created += 1
                self.log(f"Inbox: created task in [{project}]: '{task_body[:50]}...'")
                
                # Archive this line with timestamp and project (APPEND, not overwrite)
                timestamp = datetime.now().isoformat()
                with open(archive_path, "a", encoding="utf-8") as f:
                    f.write(f"\n[{timestamp}] [{project}] {task_body}")
            except Exception as e:
                self.log(f"Inbox: failed to create task: {e}")
                new_lines.append(line)
        
        # Update inbox with remaining content
        inbox_path.write_text("\n".join(new_lines), encoding="utf-8")
        
        return tasks_created
    
    def _get_active_project(self) -> str:
        """Get the currently active project name."""
        active_path = self.vault_root / "_active.md"
        if active_path.exists():
            return active_path.read_text().strip() or "default"
        return "default"
    
    # =========================================================================
    # Auto-Project Detection
    # =========================================================================
    
    def _detect_new_projects(self) -> int:
        """
        Detect new project folders and auto-populate them with skeleton.
        
        Returns number of new projects created.
        """
        projects_root = self.vault_root / "Projects"
        if not projects_root.exists():
            return 0
        
        new_projects = 0
        for project_dir in projects_root.iterdir():
            if not project_dir.is_dir():
                continue
            
            # Check if this project needs skeleton (missing tasks/pending, etc.)
            tasks_dir = project_dir / "tasks"
            if not tasks_dir.exists():
                try:
                    ensure_project_skeleton(self.vault_root, project_dir.name)
                    self.log(f"Auto-populated skeleton for project: {project_dir.name}")
                    new_projects += 1
                except Exception as e:
                    self.log(f"Failed to create skeleton for {project_dir.name}: {e}")
        
        return new_projects
    
    # =========================================================================
    # Digest Generation
    # =========================================================================
    
    def _update_digest(self, message: str, level: str = "info") -> None:
        """
        Add an entry to _digest.md with timestamp (append-only).
        
        Args:
            message: The digest message to add
            level: Log level (info, success, warning, error)
        """
        digest_path = self.vault_root / "_digest.md"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        level_icons = {
            "info": "ℹ️",
            "success": "✅",
            "warning": "⚠️",
            "error": "❌",
        }
        icon = level_icons.get(level, "ℹ️")
        entry = f"\n[{timestamp}] {icon} {message}"
        
        # Append-only write
        with open(digest_path, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    
    def _compact_digest(self) -> None:
        """Compact _digest.md if it exceeds max size."""
        digest_path = self.vault_root / "_digest.md"
        if not digest_path.exists():
            return
        
        # Archive to _archive/_digest/
        archive_dir = self.vault_root / "_archive" / "_digest"
        archive_dir.mkdir(parents=True, exist_ok=True)
        
        content = digest_path.read_text(encoding="utf-8")
        if len(content) > 50000:  # 50KB threshold
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            archive_file = archive_dir / f"digest_{timestamp}.md"
            archive_file.write_text(content, encoding="utf-8")
            # Keep last 50 entries
            lines = content.strip().split("\n")
            digest_path.write_text("\n".join(lines[-50:]) + "\n", encoding="utf-8")
            self.log(f"Archived digest to {archive_file.name}")
    
    # =========================================================================
    # STATUS.md Management
    # =========================================================================
    
    def _update_project_status(self, project_name: str, message: str) -> None:
        """Add an entry to a project's STATUS.md (append-only)."""
        status_path = self.vault_root / "Projects" / project_name / "STATUS.md"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n[{timestamp}] {message}"
        
        # Append-only write
        with open(status_path, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    
    def _compact_status(self, project_name: str) -> None:
        """Compact STATUS.md if it exceeds max size."""
        status_path = self.vault_root / "Projects" / project_name / "STATUS.md"
        if not status_path.exists():
            return
        
        # Archive to _archive/<project>/
        archive_dir = self.vault_root / "_archive" / project_name
        archive_dir.mkdir(parents=True, exist_ok=True)
        
        content = status_path.read_text(encoding="utf-8")
        if len(content) > 50000:  # 50KB threshold
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            archive_file = archive_dir / f"status_{timestamp}.md"
            archive_file.write_text(content, encoding="utf-8")
            # Keep last 100 entries
            lines = content.strip().split("\n")
            status_path.write_text("\n".join(lines[-100:]) + "\n", encoding="utf-8")
            self.log(f"Archived STATUS.md for {project_name}")
    
    # =========================================================================
    # Task Execution
    # =========================================================================
    
    def execute_task(self, task_path: Path, worker=None) -> ExecutionResult:
        """
        Execute a single task with an available worker.
        
        Features:
        - Smart task routing (routes by task_type to matching workers)
        - Task timeout enforcement
        - Cost tracking
        - Audit logging
        - Changeset handoff manifest
        - Deterministic verify-before-final gate for coding tasks
        
        worker: Optional pre-reserved worker. If provided, uses that worker directly.
        """
        max_attempts = self.config.get("max_task_attempts", 3)
        max_turns = self.config.get("max_turns", 6)
        execute_timeout = self.config.get("execute_timeout", 30)
        
        # Parse task
        task = parse_task(task_path)
        
        # Determine project path
        project_path = task_path.parent.parent.parent / "Projects" / task_path.parent.parent.name
        if not project_path.exists():
            project_path = task_path.parent.parent.parent / "Projects" / "default"
        project_name = project_path.name
        
        # Claim task
        claimed = claim_task(task_path, self.instance_id)
        if not claimed:
            self.audit.log("task_claim_failed", f"Failed to claim task", task=str(task_path))
            return ExecutionResult(success=False, output="Failed to claim task", changes=[])
        
        task_path = claimed
        
        # Update attempts - re-parse after claim to get fresh state
        from .tasks import set_meta
        task = parse_task(task_path)
        task.attempts += 1
        new_txt = set_meta(task.raw_text, task.task_type, task.attempts)
        task_path.write_text(new_txt)
        
        self.log(f"Executing: {task.body[:50]}... (attempt {task.attempts}/{max_attempts})")
        self.audit.log("task_start", f"Started task execution",
                      task=str(task_path), type=task.task_type, project=project_name,
                      attempt=task.attempts)
        
        # Snapshot before execution
        snapshot_path = self.rollback.snapshot(project_path, f"task-{task_path.stem[:8]}")
        
        # Track timing
        start_time = time.time()
        total_cost = 0.0
        
        # Track if verification was called (for deterministic verify gate)
        verification_called = False
        
        # Get prior log for changeset seeding (if this is attempt > 1)
        prior_log = None
        if task.attempts > 1:
            # Try to read prior attempt's transcript
            backup_dir = self.vault_root / "_backups"
            for bf in sorted(backup_dir.glob(f"progress_{task_path.stem}_*.json"), reverse=True)[:3]:
                try:
                    data = json.loads(bf.read_text())
                    # Check if this has a changeset
                    content = json.dumps(data)
                    if "Files Changed So Far" in content or "created" in content:
                        prior_log = content
                        break
                except Exception:
                    pass
        
        try:
            # Build context
            relevant_context = self._get_relevant_context(project_path, task.body)
            
            # Use template-based prompt with task type
            prompt = self._get_prompt(task.task_type, task.body, relevant_context)
            
            # Execute agent loop with task type and timeout
            # Pass the pre-reserved worker if available
            success, output, changes, cost, changed_paths = self._agent_loop(
                prompt=prompt,
                project_path=project_path,
                max_turns=max_turns,
                execute_timeout=execute_timeout,
                task_id=task_path.stem,
                task_type=task.task_type,
                timeout_seconds=self._task_timeout,
                prior_log=prior_log,
                worker=worker,
            )
            
            total_cost = cost
            duration = time.time() - start_time
            
            # P1: Retrieval hit-rate metric
            if changed_paths or read_files:
                indexer = self._get_indexer(project_path)
                selected = set(f.path for f in indexer.find_relevant(task.body, top_n=5))
                actually_used = set(read_files.keys()) | set(changed_paths.keys())
                if selected and actually_used:
                    precision = len(selected & actually_used) / len(selected)
                    recall = len(selected & actually_used) / len(actually_used)
                    self._logger.info("retrieval_metrics",
                                     task_id=task_path.stem,
                                     precision=round(precision, 2),
                                     recall=round(recall, 2),
                                     selected=len(selected),
                                     used=len(actually_used))
            
            # Record cost
            if total_cost > 0:
                self.costs.record(project_name, "unknown", total_cost)  # Model recorded in _agent_loop
            
            # Verification (deterministic gate for coding tasks)
            if success:
                verification_called = True  # Mark that we ran verify
                result = verify(project_path)
                
                # For coding tasks, verify() must pass
                # This is per SPEC.md: "deterministic software performs...validation"
                if task.task_type == "coding" and not result.passed:
                    success = False
                    output = f"Verification failed:\n{result.output}"
                    verification_called = True
                elif not result.passed:
                    # For other types, log but don't fail
                    self._logger.warning("Verification warning", task_id=task_path.stem, 
                                        output=result.output)
            
            # Handle failure
            if not success:
                if snapshot_path:
                    self.rollback.rollback(snapshot_path, project_path)
                
                self.audit.log("task_fail", f"Task failed",
                              task=str(task_path), project=project_name,
                              attempt=task.attempts, cost_usd=total_cost,
                              duration_seconds=duration,
                              verification_called=verification_called)
                
                if task.attempts >= max_attempts:
                    result_text = output[:200]
                    # Include changeset summary in failure
                    if changed_paths:
                        result_text += "\n" + format_changeset_summary(changed_paths)
                    finish_task(task_path, success=False, result=result_text)
                    self.log(f"Failed after {max_attempts} attempts")
                    return ExecutionResult(success=False, output=output, changes=changes,
                                        cost_usd=total_cost, duration_seconds=duration)
                
                # Release back to pending for retry
                # Include changeset in the retry transcript
                self._append_changeset_to_transcript(task_path.stem, changed_paths)
                release_task(task_path, back_to_pending=True)
                return ExecutionResult(success=False, output=output, changes=changes,
                                    cost_usd=total_cost, duration_seconds=duration)
            
            # Success
            result_text = output[:200]
            # Include changeset summary for human-facing record
            if changed_paths:
                result_text += "\n" + format_changeset_summary(changed_paths)
            finish_task(task_path, success=True, result=result_text)
            self.log(f"Completed: {task.body[:50]}...")
            self.audit.log("task_complete", f"Task completed successfully",
                          task=str(task_path), project=project_name,
                          cost_usd=total_cost, duration_seconds=duration,
                          changes=len(changes),
                          files=list(changed_paths.keys()) if changed_paths else None)
            return ExecutionResult(success=True, output=output, changes=changes,
                                cost_usd=total_cost, duration_seconds=duration)
        
        except Exception as e:
            duration = time.time() - start_time
            self.log(f"Error: {e}")
            self.audit.log("task_error", f"Task error: {e}",
                          task=str(task_path), project=project_name,
                          duration_seconds=duration)
            if snapshot_path:
                self.rollback.rollback(snapshot_path, project_path)
            finish_task(task_path, success=False, result=str(e))
            return ExecutionResult(success=False, output=str(e), changes=[],
                                cost_usd=total_cost, duration_seconds=duration)
    
    def _append_changeset_to_transcript(self, task_id: str, changed_paths: dict[str, str]) -> None:
        """
        Append the changeset manifest to the latest transcript backup.
        
        This ensures the next attempt can read the changeset and continue
        tracking changes across the task's full lifetime.
        """
        if not changed_paths:
            return
        
        backup_dir = self.vault_root / "_backups"
        changeset_text = format_changeset(changed_paths)
        
        # Find the most recent transcript
        transcripts = sorted(backup_dir.glob(f"progress_{task_id}_*.json"), reverse=True)
        if not transcripts:
            return
        
        try:
            latest = transcripts[0]
            data = json.loads(latest.read_text())
            # Append changeset to last_response if it exists
            if "last_response" in data and data["last_response"]:
                data["last_response"] += "\n" + changeset_text
            latest.write_text(json.dumps(data, indent=2))
        except Exception as e:
            self._logger.warning("Failed to append changeset to transcript", 
                              task_id=task_id, error=str(e))
    
    def _agent_loop(
        self,
        prompt: str,
        project_path: Path,
        max_turns: int,
        execute_timeout: int,
        task_id: str | None = None,
        task_type: str = "general",
        timeout_seconds: int = DEFAULT_TASK_TIMEOUT_SECONDS,
        prior_log: str | None = None,
        worker=None,
    ) -> tuple[bool, str, list[str], float, dict[str, str]]:
        """
        Run the AI agent loop.
        
        Features:
        - Smart task routing (routes by task_type to matching workers)
        - Task timeout enforcement
        - Cost tracking
        - Changeset handoff manifest (tracks files across attempts)
        - Observation cache for deterministic lookups (per-attempt)
        
        worker: Optional pre-reserved worker. If provided, uses that worker directly
        instead of re-selecting from the pool.
        
        Returns: (success, output, changed_files, total_cost, changed_paths)
        """
        # Clear knowledge provider cache for this project at the start of each attempt
        project_key = str(project_path)
        if project_key in self._knowledge_providers:
            self._knowledge_providers[project_key].clear_cache()
        
        # Seed changeset from prior log if available
        changed_paths: dict[str, str] = {}
        if prior_log:
            changed_paths = parse_changeset_from_log(prior_log)
        
        messages = [{"role": "system", "content": prompt}]
        changed_files: list[str] = []
        # Track read files: path -> (was_truncated, total_size)
        read_files: dict[str, tuple[bool, int]] = {}
        total_cost = 0.0
        loop_start_time = time.time()
        last_model = "unknown"
        
        for turn in range(max_turns):
            # Check overall task timeout
            elapsed = time.time() - loop_start_time
            if elapsed >= timeout_seconds:
                self._logger.warning("Task timeout exceeded", 
                                   task_id=task_id, elapsed_seconds=elapsed,
                                   timeout_seconds=timeout_seconds)
                return False, f"Task timeout after {int(elapsed)}s", changed_files, total_cost, changed_paths
            
            # Call AI - use pre-reserved worker if available, otherwise pool selection
            try:
                if worker is not None:
                    # Use the pre-reserved worker directly
                    worker, response, cost = self.pool.call_with(worker, messages)
                else:
                    # Fall back to pool selection
                    worker, response, cost = self.pool.call(task_type, messages)
                total_cost += cost
                last_model = worker.model
            except RuntimeError as e:
                return False, f"No workers available: {e}", changed_files, total_cost, changed_paths
            
            # Persist turn progress (CRASH RESILIENCE per SPEC.md)
            self.persist_turn_progress(task_id, turn, messages, response)
            
            # Parse action
            action = parse_action(response)
            if not action:
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": "Invalid response. Return JSON with action field."})
                continue
            
            # Execute action (pass changeset tracker)
            obs = self._execute_action(action, project_path, execute_timeout, read_files, changed_files, changed_paths)
            
            # Persist after action too
            self.persist_turn_progress(task_id, turn, messages, obs, is_observation=True)
            
            # Check for final
            if action.action == "final":
                return True, action.result or obs, changed_files, total_cost, changed_paths
            
            # Continue
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": obs})
            
            # P2: Compress stale read observations to prevent context bloat
            if len(messages) > 6:
                self._compress_stale_reads(messages)
        
        return False, f"Hit {max_turns} turn limit", changed_files, total_cost, changed_paths
    
    def persist_turn_progress(
        self,
        task_id: str | None,
        turn: int,
        messages: list[dict],
        last_response: str,
        is_observation: bool = False,
    ) -> None:
        """
        Write transcript to disk after every turn (CRASH RESILIENCE per SPEC.md).
        
        Uses one file per task_id, overwritten each turn (not timestamped).
        """
        if task_id is None:
            return
        
        backup_dir = self.vault_root / "_backups"
        backup_dir.mkdir(exist_ok=True)
        
        # One file per task_id, overwritten each turn
        progress_file = backup_dir / f"progress_{task_id}.json"
        
        data = {
            "task_id": task_id,
            "turn": turn,
            "is_observation": is_observation,
            "timestamp": datetime.now().isoformat(),
            "messages": messages,
            "last_response": last_response,
        }
        
        try:
            progress_file.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass  # Don't fail on persistence errors
    
    def _compress_stale_reads(self, messages: list[dict]) -> None:
        """Replace old read observations with one-line markers."""
        read_paths: set[str] = set()
        # Scan from end to find most recent reads
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if content.startswith("OK: read ") or content.startswith("OK: wrote "):
                    # Extract path from observation
                    parts = content.split()
                    if len(parts) >= 3:
                        read_paths.add(parts[2])
                elif content.startswith("FILE: "):
                    # From get_context output
                    path = content.split("\n")[0].replace("FILE: ", "").strip()
                    read_paths.add(path)
        
        # Compress older reads that are superseded
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if "FILE:" in content and len(content) > 500:
                    # Extract filename from context block
                    first_line = content.split("\n")[0]
                    if "FILE:" in first_line:
                        path = first_line.replace("FILE: ", "").replace("=", "").strip()
                        if path in read_paths:
                            msg["content"] = f"[Previously read: {path}]"
    
    def _execute_action(
        self,
        action: Action,
        project_path: Path,
        timeout: int,
        read_files: dict[str, tuple[bool, int]],
        changed_files: list[str],
        changed_paths: dict[str, str] | None = None,
    ) -> str:
        """
        Execute a single action.
        
        changed_paths: Optional dict to track created vs overwritten status.
        """
        
        if action.action == "list":
            # Return shallow map of vault (per SPEC.md)
            parts = ["# Vault Structure"]
            for p in sorted(project_path.rglob("*")):
                if p.is_file() and not any(s in p.parts for s in ["__pycache__", ".git", "node_modules"]):
                    depth = len(p.relative_to(project_path).parts) - 1
                    prefix = "  " * depth + ("📄 " if p.suffix in [".py", ".js", ".ts", ".md"] else "📁 ")
                    parts.append(f"{prefix}{p.name}")
                elif p.is_dir() and not any(s in p.parts for s in ["__pycache__", ".git", "node_modules", "tasks"]):
                    depth = len(p.relative_to(project_path).parts)
                    parts.append("  " * depth + "📂/")
            return "\n".join(parts[:100])  # Limit output
        
        elif action.action == "ask_human":
            # Park task in waiting/ for human response (per SPEC.md)
            from .tasks import release_task
            release_task(project_path / "tasks" / "doing" / f"{action.path or 'current'}.md", 
                        back_to_pending=False)  # back_to_pending=False goes to waiting/
            return "PARKED: Task moved to waiting/ - await human response"
        
        elif action.action == "read":
            if not action.path:
                return "ERROR: no path"
            target = safe_vault_path(project_path, action.path)
            if not target.exists():
                return f"ERROR: not found: {action.path}"
            
            # Read the file content
            content = target.read_text(encoding="utf-8", errors="replace")
            total_len = len(content)
            
            # Track whether fully read or truncated
            if total_len > 3000:
                shown_content = content[:3000]
                truncated = True
                # Store as tuple: (was_truncated, total_size)
                read_files[action.path] = (True, total_len)
                return shown_content + f"\n\n...[TRUNCATED: {total_len - 3000} of {total_len} chars hidden]"
            else:
                read_files[action.path] = (False, total_len)
                return content
        
        elif action.action == "write":
            if not action.path:
                return "ERROR: no path"
            
            target = safe_vault_path(project_path, action.path)
            
            if target.exists():
                # File exists - check if it was read
                if action.path not in read_files:
                    return f"REFUSED: overwrite without read first"
                
                # Check if it was fully read or truncated
                read_info = read_files.get(action.path)
                if read_info and read_info[0]:  # was truncated
                    return f"REFUSED: file was only partially read ({read_info[1]} bytes). Read the full file first."
            
            # Track created vs overwritten for changeset
            status = "overwritten" if target.exists() else "created"
            
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action.content or "", encoding="utf-8")
            changed_files.append(action.path)
            
            # Update changeset manifest
            if changed_paths is not None:
                changed_paths[action.path] = status
            
            # Invalidate knowledge provider cache for this file
            rel_path = str(target.relative_to(project_path))
            project_key = str(project_path)
            if project_key in self._knowledge_providers:
                self._knowledge_providers[project_key]._cache.invalidate_file(rel_path)
            
            return f"OK: wrote {len(action.content or '')} chars"
        
        elif action.action == "apply_patch":
            """Apply a targeted patch to a file (search/replace)."""
            if not action.path:
                return "ERROR: no path"
            
            target = safe_vault_path(project_path, action.path)
            
            # File must exist and must have been read first
            if not target.exists():
                return f"ERROR: file not found: {action.path}"
            
            if action.path not in read_files:
                return f"REFUSED: patch without read first"
            
            # Get old text (content field) and new text (result field)
            old_text = action.content
            new_text = action.result
            
            if not old_text:
                return "ERROR: apply_patch requires content field with text to replace"
            
            # Read current file
            current_content = target.read_text(encoding="utf-8", errors="replace")
            
            # Apply patch
            if old_text not in current_content:
                return f"ERROR: patch target not found. The old_text must match exactly."
            
            # Apply the patch
            new_content = current_content.replace(old_text, new_text, 1)
            target.write_text(new_content, encoding="utf-8")
            
            changed_files.append(action.path)
            
            # Update changeset manifest
            if changed_paths is not None:
                changed_paths[action.path] = "overwritten"
            
            # Invalidate knowledge provider cache for this file
            rel_path = str(target.relative_to(project_path))
            project_key = str(project_path)
            if project_key in self._knowledge_providers:
                self._knowledge_providers[project_key]._cache.invalidate_file(rel_path)
            
            return f"OK: patched {len(old_text)} → {len(new_text)} chars"
        
        elif action.action == "apply_multi_patch":
            """
            Apply multiple patches atomically. All files must pass containment check.
            If any file fails validation, the entire operation is rejected.
            
            Expects action.content to be a JSON list of patches:
            [{"path": "file1", "old_text": "...", "new_text": "..."}, ...]
            """
            if not action.content:
                return "ERROR: apply_multi_patch requires content with JSON list of patches"
            
            try:
                patches = json.loads(action.content)
            except json.JSONDecodeError:
                return "ERROR: apply_multi_patch content must be valid JSON"
            
            if not isinstance(patches, list):
                return "ERROR: apply_multi_patch content must be a JSON list"
            
            # Validate all paths first (fail-fast on containment check)
            for patch in patches:
                if not isinstance(patch, dict):
                    return "ERROR: each patch must be a JSON object"
                if "path" not in patch:
                    return "ERROR: each patch must have a 'path' field"
                
                target = safe_vault_path(project_path, patch["path"])
                if not target.exists():
                    return f"ERROR: file not found: {patch['path']}"
                
                if patch["path"] not in read_files:
                    return f"REFUSED: patch without read first: {patch['path']}"
            
            # All validations passed - apply patches
            results = []
            for patch in patches:
                target = safe_vault_path(project_path, patch["path"])
                current = target.read_text(encoding="utf-8", errors="replace")
                
                old_text = patch.get("old_text", "")
                new_text = patch.get("new_text", "")
                
                if old_text not in current:
                    # Rollback already-applied patches would be complex,
                    # so we fail the whole operation if any patch doesn't match
                    return f"ERROR: patch target not found in {patch['path']}. All patches rejected."
                
                new_content = current.replace(old_text, new_text, 1)
                target.write_text(new_content, encoding="utf-8")
                changed_files.append(patch["path"])
                
                if changed_paths is not None:
                    changed_paths[patch["path"]] = "overwritten"
                
                results.append(f"{patch['path']}: {len(old_text)} → {len(new_text)}")
            
            # Invalidate knowledge provider cache for all patched files
            project_key = str(project_path)
            if project_key in self._knowledge_providers:
                for patch in patches:
                    rel_path = patch["path"]
                    self._knowledge_providers[project_key]._cache.invalidate_file(rel_path)
            
            return "OK: " + "; ".join(results)
        
        elif action.action == "find_references":
            """Find files that define or reference a symbol."""
            if not action.path:
                return "ERROR: find_references requires path (the symbol to search for)"
            
            # Get or create knowledge provider for this project
            project_key = str(project_path)
            if project_key not in self._knowledge_providers:
                if project_key not in self._index_cache:
                    self._index_cache[project_key] = Indexer(project_path)
                    self._index_cache[project_key].build()
                self._knowledge_providers[project_key] = KnowledgeProvider(self._index_cache[project_key])
            
            provider = self._knowledge_providers[project_key]
            results = provider.find_references(action.path)
            
            if not results:
                return f"No references found for '{action.path}'"
            
            lines = [f"# References to '{action.path}'"]
            for entry in results:
                lines.append(f"- {entry.path} ({entry.line_count} lines)")
                if entry.symbols:
                    syms = [s for s in entry.symbols if action.path in s]
                    if syms:
                        lines.append(f"  Symbols: {', '.join(syms[:5])}")
            
            return "\n".join(lines)
        
        elif action.action == "find_definition":
            """Find files that define a symbol."""
            if not action.path:
                return "ERROR: find_definition requires path"
            
            # Get or create knowledge provider for this project
            project_key = str(project_path)
            if project_key not in self._knowledge_providers:
                if project_key not in self._index_cache:
                    self._index_cache[project_key] = Indexer(project_path)
                    self._index_cache[project_key].build()
                self._knowledge_providers[project_key] = KnowledgeProvider(self._index_cache[project_key])
            
            provider = self._knowledge_providers[project_key]
            results = provider.find_definition(action.path)
            
            if not results:
                return f"No definition found for '{action.path}'"
            
            lines = [f"# Definition of '{action.path}'"]
            for entry in results:
                lines.append(f"- {entry.path} ({entry.line_count} lines)")
            
            return "\n".join(lines)
        
        elif action.action == "find_importers":
            """Find files that import a module."""
            if not action.path:
                return "ERROR: find_importers requires path"
            
            # Get or create knowledge provider for this project
            project_key = str(project_path)
            if project_key not in self._knowledge_providers:
                if project_key not in self._index_cache:
                    self._index_cache[project_key] = Indexer(project_path)
                    self._index_cache[project_key].build()
                self._knowledge_providers[project_key] = KnowledgeProvider(self._index_cache[project_key])
            
            provider = self._knowledge_providers[project_key]
            results = provider.find_importers(action.path)
            
            if not results:
                return f"No importers found for '{action.path}'"
            
            lines = [f"# Importers of '{action.path}'"]
            for entry in results:
                lines.append(f"- {entry.path} ({entry.line_count} lines)")
            
            return "\n".join(lines)
        
        elif action.action == "find_tests":
            """Find test files related to a symbol or file."""
            if not action.path:
                return "ERROR: find_tests requires path"
            
            # Get or create indexer for this project
            project_key = str(project_path)
            if project_key not in self._index_cache:
                self._index_cache[project_key] = Indexer(project_path)
                self._index_cache[project_key].build()
            
            indexer = self._index_cache[project_key]
            results = indexer.find_tests(action.path)
            
            if not results:
                return f"No test files found related to '{action.path}'"
            
            lines = [f"# Tests for '{action.path}'"]
            for entry in results:
                lines.append(f"- {entry.path} ({entry.line_count} lines)")
            
            return "\n".join(lines)
        
        elif action.action == "find_imports":
            """Find import dependencies of a file."""
            if not action.path:
                return "ERROR: find_imports requires path"
            
            target = safe_vault_path(project_path, action.path)
            if not target.exists():
                return f"ERROR: file not found: {action.path}"
            
            # Get or create indexer for this project
            project_key = str(project_path)
            if project_key not in self._index_cache:
                self._index_cache[project_key] = Indexer(project_path)
                self._index_cache[project_key].build()
            
            indexer = self._index_cache[project_key]
            rel = str(target.relative_to(project_path))
            
            if rel not in indexer.files:
                return f"ERROR: file not indexed: {action.path}"
            
            entry = indexer.files[rel]
            if not entry.imports:
                return f"No imports found in '{action.path}'"
            
            lines = [f"# Imports in '{action.path}'"]
            for imp in entry.imports[:20]:
                lines.append(f"- {imp}")
            
            return "\n".join(lines)
        
        elif action.action == "execute":
            if not action.command:
                return "ERROR: no command"
            
            # Execute is opt-in per SPEC.md - must be explicitly enabled
            if not self._allow_execute:
                return "REFUSED: execute is disabled for this project. Set allow_execute: true in config.json to enable."
            
            result = execute(action.command, project_path, timeout=timeout)
            return f"exit {result.exit_code}\n{result.output}"
        
        elif action.action == "final":
            return f"Final: {action.result}"
        
        return f"ERROR: unknown action: {action.action}"
    
    # =========================================================================
    # Main Loop
    # =========================================================================
    
    def start(self) -> None:
        """Start the orchestrator."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.log("Orchestrator started")

    def stop(self) -> None:
        """Stop the orchestrator."""
        self._stop.set()
        self.log("Orchestrator stopping")

    def _run(self) -> None:
        """Main polling loop."""
        # Track cycle count for periodic tasks
        cycle_count = 0
        
        while not self._stop.is_set():
            cycle_count += 1
            
            # === Check pause state ===
            self._pause.wait()  # Block if paused
            
            # === Check for config hot reload (every 10 cycles ≈ 10 seconds) ===
            if cycle_count % 10 == 0:
                self._check_config_reload()
            
            # === Periodic housekeeping (every 60 cycles ≈ 1 minute) ===
            if cycle_count % 60 == 0:
                # Detect new projects
                new_proj = self._detect_new_projects()
                if new_proj:
                    self._update_digest(f"Auto-populated {new_proj} new project(s)", "info")
                
                # Compact digest if needed
                self._compact_digest()
                
                # Compact STATUS.md for all projects
                for project_dir in list_project_dirs(self.vault_root):
                    self._compact_status(project_dir.name)
            
            # === Process inbox (every 10 cycles ≈ 10 seconds) ===
            if cycle_count % 10 == 0:
                inbox_tasks = self._process_inbox()
                if inbox_tasks > 0:
                    self._update_digest(f"Created {inbox_tasks} task(s) from inbox", "info")
            
            # === Recover stale tasks ===
            self._sweep_stale_claims(max_age_minutes=self.config.get("stale_claim_minutes", 30))
            
            # === Scan pending tasks and build graph ===
            pending_paths = scan_pending_tasks(self.vault_root)
            graph = build_graph_from_paths(pending_paths)
            
            # Check for cycles
            if graph.detect_cycle():
                self.log("WARNING: Dependency cycle detected")
                self._update_digest("Dependency cycle detected in task graph", "warning")
            
            # Get ready tasks
            ready = graph.get_ready()
            
            # Dispatch ready tasks to idle workers (parallel execution)
            for task_node in ready:
                # Get task type for smart routing
                task = parse_task(task_node.path)
                task_type = task.task_type if task.task_type else "general"
                
                # Smart routing: get worker that can handle this task type
                worker = self.pool.get_idle(task_type)
                if not worker:
                    break  # No more idle workers available
                
                worker.start_job(task_node.id)
                
                # Execute in background
                threading.Thread(
                    target=self._execute_and_release,
                    args=(task_node.path, worker),
                    daemon=True,
                ).start()
            
            time.sleep(1)

    def _execute_and_release(self, task_path: Path, worker) -> None:
        """Execute task and release worker."""
        success = False
        task_name = task_path.stem
        duration = 0.0
        cost = 0.0
        
        try:
            # Pass the reserved worker to ensure it handles this task
            result = self.execute_task(task_path, worker=worker)
            success = result.success
            duration = result.duration_seconds
            cost = result.cost_usd
            
            # Update digest and project status
            if success:
                self._update_digest(f"Completed task: {task_name}", "success")
                # Determine project from path
                if "/Projects/" in str(task_path):
                    parts = task_path.parts
                    proj_idx = parts.index("Projects") + 1
                    project = parts[proj_idx] if proj_idx < len(parts) else "default"
                    self._update_project_status(project, f"✅ Completed: {task_name}")
            else:
                self._update_digest(f"Failed task: {task_name}", "error")
                if "/Projects/" in str(task_path):
                    parts = task_path.parts
                    proj_idx = parts.index("Projects") + 1
                    project = parts[proj_idx] if proj_idx < len(parts) else "default"
                    self._update_project_status(project, f"❌ Failed: {task_name}")
        except Exception as e:
            success = False
            self._update_digest(f"Task error {task_name}: {e}", "error")
        finally:
            worker.finish_job(success=success, duration=duration, cost_usd=cost)

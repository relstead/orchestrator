"""
Orchestrator module for Vault Orchestrator.

Main orchestrator that ties all components together.
Per §13.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .config import Config
    from .vault import Vault
    from .worker import WorkerPool
    from .tasks import Task, TaskStore, TaskState, TaskType
    from .logger import MetricsWriter, DigestWriter
    from .indexer import Indexer
    from .agent import Action, TaskTranscript, Turn
    from .dependency import DependencyGraph
    from .planner import PlannerValidator
    from .sandbox import SpawnResult


@dataclass
class OrchestratorState:
    """Runtime state of the orchestrator."""
    running: bool = True
    poll_count: int = 0
    last_inbox_process: float = 0
    last_compaction: float = 0
    compaction_backoff_until: float = 0
    inbox_backoff_until: float = 0
    orphaned_claims: list[str] = field(default_factory=list)
    # Snapshot tracking per §8.12
    snapshot_taken_for: dict[str, tuple[int, str]] = field(default_factory=dict)  # task_id -> (attempt, snapshot_path)
    # Changeset tracking per §9.2
    changeset: dict[str, dict] = field(default_factory=dict)  # path -> {status, attempt}
    compaction_ran_this_cycle: bool = False  # Per §9.4: one compaction per cycle


class Orchestrator:
    """
    Main orchestrator for the Vault Orchestrator system.
    
    Coordinates all components and runs the main loop.
    """
    
    def __init__(self, vault_path: Path, config: "Config"):
        from .vault import open_vault
        from .worker import WorkerPool
        from .tasks import TaskStore
        from .logger import MetricsWriter, DigestWriter
        from .indexer import get_indexer
        from .planner import PlannerValidator
        from .sandbox import AppContainerSandbox
        
        self._vault_path = vault_path
        self._config = config
        
        # Initialize components
        self._vault = open_vault(vault_path)
        self._workers = WorkerPool(config)
        self._task_store = TaskStore(self._vault)
        self._metrics = MetricsWriter(vault_path)
        self._digest = DigestWriter(
            self._vault.digest_path,
            self._vault.archive_digest_path,
        )
        self._indexer = get_indexer(self._vault)
        self._planner = PlannerValidator(config)
        
        # Sandbox (will be initialized per §0.5)
        self._sandbox: AppContainerSandbox | None = None
        self._sandbox_initialized = False
        
        # Runtime state
        self._state = OrchestratorState()
        
        # Callbacks
        self._on_task_complete: Callable | None = None
        self._on_task_fail: Callable | None = None
    
    @property
    def vault(self) -> "Vault":
        return self._vault
    
    @property
    def config(self) -> "Config":
        return self._config
    
    @property
    def workers(self) -> "WorkerPool":
        return self._workers
    
    @property
    def task_store(self) -> "TaskStore":
        return self._task_store
    
    @property
    def metrics(self) -> "MetricsWriter":
        return self._metrics
    
    def initialize_sandbox(self) -> bool:
        """
        Initialize the sandbox per §0.5.
        
        This implements the build order directive:
        1. Job Object (always)
        2. Restricted Token fallback
        3. AppContainer profile
        4. ACL setup
        5. Pipe capture (integrated)
        6. Full integration
        """
        from .sandbox import AppContainerSandbox
        
        if self._sandbox_initialized:
            return True
        
        try:
            self._sandbox = AppContainerSandbox("VaultOrchestrator")
            
            # Step 1-3: Create profile (includes Job Object setup)
            if not self._sandbox.create_profile():
                self._sandbox = None
                return False
            
            # Step 4: ACL setup would happen here (Windows-specific)
            # Step 5-6: Integrated into execute()
            
            self._sandbox_initialized = True
            return True
            
        except Exception as e:
            print(f"Sandbox initialization failed: {e}", file=sys.stderr)
            self._sandbox = None
            return False
    
    def run(self, poll_interval: float = 5.0) -> None:
        """
        Run the main orchestrator loop.
        
        This is the core loop that:
        1. Scans for work
        2. Dispatches tasks to workers
        3. Handles inbox and compaction
        """
        print(f"Starting orchestrator for vault: {self._vault_path}")
        print(f"Poll interval: {poll_interval}s")
        
        while self._state.running:
            try:
                self._poll()
                time.sleep(poll_interval)
            except KeyboardInterrupt:
                print("\nShutting down...")
                self._state.running = False
            except Exception as e:
                print(f"Error in poll cycle: {e}", file=sys.stderr)
                time.sleep(poll_interval)
        
        print("Orchestrator stopped.")
    
    def _poll(self) -> None:
        """Run a single poll cycle."""
        self._state.poll_count += 1
        
        # Reset per-cycle flags
        self._state.compaction_ran_this_cycle = False
        
        # 1. Stale-claim sweep - recover orphaned doing/ tasks
        self._recover_stale_claims()
        
        # 2. Process inbox
        self._process_inbox()
        
        # 3. Build dependency graph and resolve blocked tasks
        self._resolve_dependencies()
        
        # 4. Get ready tasks and dispatch
        self._dispatch_ready_tasks()
        
        # 5. Process compaction
        self._maybe_compact()
    
    def _recover_stale_claims(self) -> None:
        """
        Recover orphaned doing/ tasks.
        
        Per §15 test 17: runs on every poll cycle, not just startup.
        """
        for project in self._vault.discover_projects():
            for task in self._task_store.get_tasks_in_state(project, "doing"):
                # Check if the task has been claimed too long
                # For now, just move back to pending
                # In real impl, would check claim timestamp
                try:
                    self._task_store.move_task(task, "pending")
                except Exception:
                    pass
    
    def _process_inbox(self) -> None:
        """
        Process inbox with backoff.
        
        Parses inbox content into tasks per §9.5.
        Supports [project] and @project: syntax.
        """
        # Check backoff
        if time.time() < self._state.inbox_backoff_until:
            return
        
        if not self._vault.inbox_path.exists():
            return
        
        content = self._vault.inbox_path.read_text().strip()
        if not content:
            return
        
        # Parse inbox into task items
        task_items = self._parse_inbox_items(content)
        
        if not task_items:
            # No valid tasks found, archive
            self._archive_inbox_content(content)
            return
        
        # Create tasks from parsed items
        tasks_created = 0
        for item in task_items:
            project = item.get("project")
            title = item.get("title")
            body = item.get("body", "")
            
            if not title:
                continue
            
            # Use default project if not specified
            if not project:
                projects = self._vault.discover_projects()
                if projects:
                    project = projects[0]
                else:
                    # Create a default project
                    project = "default"
                    self._vault.ensure_project_skeleton(project)
            
            try:
                self._task_store.create_task(
                    project=project,
                    title=title[:100],  # Truncate title
                    body=body,
                    task_type=self._infer_task_type(body),
                )
                tasks_created += 1
            except Exception as e:
                print(f"Failed to create task: {e}")
        
        if tasks_created > 0:
            # Success - archive processed content
            self._archive_inbox_content(content)
            print(f"Created {tasks_created} tasks from inbox")
        else:
            # Failed to parse, set backoff and retry later
            self._state.inbox_backoff_until = time.time() + 60
    
    def _parse_inbox_items(self, content: str) -> list[dict]:
        """
        Parse inbox content into task items.
        
        Supports formats:
        - [project] Title\nBody
        - @project: Title\nBody  
        - Title\nBody (uses default project)
        - --- separator for multiple items
        """
        items = []
        current_item = None
        line_idx = 0
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        
        while line_idx < len(lines):
            line = lines[line_idx]
            
            # Check for separator
            if line.startswith("---"):
                if current_item and current_item.get("title"):
                    items.append(current_item)
                current_item = None
                line_idx += 1
                continue
            
            # Check for project prefix
            project = None
            if line.startswith("[") and "]" in line:
                project = line[1:line.index("]")]
                line = line[line.index("]") + 1:].strip()
            elif line.startswith("@") and ":" in line:
                project = line[1:line.index(":")]
                line = line[line.index(":") + 1:].strip()
            
            # Start new item with title
            title = line
            body_lines = []
            line_idx += 1
            
            # Collect body until separator or end
            while line_idx < len(lines):
                next_line = lines[line_idx]
                if next_line.startswith("---"):
                    break
                
                # Check for new task (has project prefix)
                if next_line.startswith("[") or next_line.startswith("@"):
                    break
                
                body_lines.append(next_line)
                line_idx += 1
            
            current_item = {
                "project": project,
                "title": title,
                "body": "\n".join(body_lines) if body_lines else "",
            }
            items.append(current_item)
        
        return items
    
    def _infer_task_type(self, body: str) -> str:
        """
        Infer task type from body content.
        
        Simple heuristic - plan tasks typically contain planning keywords.
        """
        body_lower = body.lower()
        
        planning_keywords = [
            "plan", "propose", "design", "architecture",
            "research", "investigate", "explore", "evaluate",
            "break down", "subtask", "milestone",
        ]
        
        for keyword in planning_keywords:
            if keyword in body_lower:
                return "plan"
        
        return "coding"
    
    def _archive_inbox_content(self, content: str) -> None:
        """Archive inbox content to _inbox_archive.md."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        
        archive = self._vault.inbox_archive_path
        archive.parent.mkdir(parents=True, exist_ok=True)
        
        with open(archive, "a") as f:
            f.write(f"[{timestamp}]\n{content}\n\n")
        
        # Clear inbox
        self._vault.inbox_path.write_text("")
        
        # Set backoff
        self._state.inbox_backoff_until = time.time() + 60
    
    def _resolve_dependencies(self) -> None:
        """Build dependency graph and move blocked tasks."""
        from .dependency import build_graph, resolve_blocked_tasks
        
        projects = self._vault.discover_projects()
        graph, blocked = build_graph(self._task_store, projects)
        
        # Move blocked tasks
        resolve_blocked_tasks(self._task_store, graph)
    
    def _dispatch_ready_tasks(self) -> None:
        """Dispatch ready tasks to available workers."""
        import asyncio
        from .tasks import TaskState
        from .worker import execute_task, TaskContext
        
        for project in self._vault.discover_projects():
            # Get ready tasks from dependency graph
            for task in self._task_store.get_tasks_in_state(project, "pending"):
                # Check if worker is available
                worker = self._workers.get_available_worker(task.meta.type.value)
                if not worker:
                    continue
                
                # Claim task (move to doing)
                try:
                    self._task_store.move_task(task, TaskState.DOING)
                    self._workers.mark_used(worker.name)
                    
                    # Build task context
                    project_path = self._vault.get_project_path(project)
                    changeset = self.load_changeset(task.id)
                    
                    context = TaskContext(
                        task_id=task.id,
                        project=project,
                        task_type=task.meta.type.value,
                        title=task.title,
                        body=task.body,
                        attempt=task.meta.attempts,
                        vault_root=self._vault_path,
                        changed_paths=changeset,
                    )
                    
                    # Take snapshot before execution (per §8.12)
                    self.ensure_snapshot(task.id, task.meta.attempts, project_path)
                    
                    # Execute task synchronously in this poll cycle
                    # For async execution, would spawn a thread/task
                    result = asyncio.run(execute_task(context, self._config))
                    
                    # Handle result
                    if result.outcome == "done":
                        self._task_store.move_task(task, TaskState.DONE)
                        self._log_task_completion(task, result)
                    elif result.outcome == "timeout":
                        # Move to waiting for retry
                        self._task_store.move_task(task, TaskState.WAITING)
                        self._log_task_timeout(task, result)
                    else:
                        # Error or failed - move to failed or retry
                        if task.meta.attempts >= self._config.budgets.max_attempts:
                            self._task_store.move_task(task, TaskState.FAILED)
                        else:
                            self._task_store.move_task(task, TaskState.WAITING)
                        self._log_task_error(task, result)
                    
                    # Save changeset
                    self.save_changeset(task.id)
                    
                    print(f"Task {task.id}: {result.outcome} ({result.turns_used} turns)")
                    
                except Exception as e:
                    print(f"Failed to dispatch task {task.id}: {e}")
    
    def _maybe_compact(self) -> None:
        """
        Maybe run compaction with backoff and cap.
        
        Per §9.4: at most one compaction per poll cycle across all projects.
        """
        # Check if already ran this cycle
        if self._state.compaction_ran_this_cycle:
            return
        
        # Check backoff
        if time.time() < self._state.compaction_backoff_until:
            return
        
        if not self._metrics.should_compact():
            return
        
        # Compact metrics
        count = self._metrics.compact()
        if count > 0:
            print(f"Compacted {count} events to archive")
            self._state.compaction_backoff_until = time.time() + 60
            self._state.compaction_ran_this_cycle = True  # Mark as run this cycle
    
    # =========================================================================
    # Snapshot & Rollback - §8.12
    # =========================================================================
    
    def ensure_snapshot(self, task_id: str, attempt: int, project_path: Path) -> str | None:
        """
        Take a snapshot before first execute of a task attempt.
        
        Per §8.12: Snapshot before first execute per attempt.
        Copies project directory (excluding tasks/) to _backups/snapshot_<task_id>_<attempt>/
        
        Returns the snapshot path, or None if already taken for this task+attempt.
        """
        # Check if snapshot already taken for this task+attempt
        key = (task_id, attempt)
        if task_id in self._state.snapshot_taken_for:
            cached_attempt, cached_path = self._state.snapshot_taken_for[task_id]
            if cached_attempt == attempt:
                return cached_path  # Already taken
        
        # Take snapshot
        snapshot_dir = self._vault.backups_path / f"snapshot_{task_id}_{attempt}"
        
        # Exclude tasks/ directory
        exclude_patterns = ['tasks', '.git', '__pycache__', '*.pyc']
        
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        
        shutil.copytree(
            project_path,
            snapshot_dir,
            dirs_exist_ok=False,
            ignore=shutil.ignore_patterns(*exclude_patterns),
        )
        
        self._state.snapshot_taken_for[task_id] = (attempt, str(snapshot_dir))
        return str(snapshot_dir)
    
    def rollback_to_snapshot(self, task_id: str) -> bool:
        """
        Rollback to the latest snapshot for a task.
        
        Returns True if rollback was performed.
        """
        if task_id not in self._state.snapshot_taken_for:
            return False
        
        attempt, snapshot_path = self._state.snapshot_taken_for[task_id]
        snapshot_dir = Path(snapshot_path)
        
        if not snapshot_dir.exists():
            return False
        
        # Find project path
        project = None
        for p in self._vault.discover_projects():
            if task_id.startswith(p):
                project = p
                break
        
        if not project:
            return False
        
        project_path = self._vault.get_project_path(project)
        
        # Restore from snapshot
        for item in snapshot_dir.iterdir():
            dest = project_path / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        
        return True
    
    # =========================================================================
    # Changeset Tracking - §9.2
    # =========================================================================
    
    def track_write(self, path: str, status: str = "created") -> None:
        """
        Track a successful write action in the changeset.
        
        Per §9.2: Records path -> {status, attempt} for multi-attempt handoff.
        """
        from .tasks import TaskState
        
        # Determine current attempt from latest task context
        attempt = 1
        for task_id, info in list(self._state.changeset.items()):
            if "attempt" in info:
                attempt = max(attempt, info["attempt"] + 1)
        
        self._state.changeset[path] = {
            "status": status,
            "attempt": attempt,
            "timestamp": time.time(),
        }
    
    def track_overwrite(self, path: str) -> None:
        """Track an overwrite (existing file modified)."""
        self._state.changeset[path] = {
            "status": "overwritten",
            "attempt": self._get_current_attempt(),
            "timestamp": time.time(),
        }
    
    def _get_current_attempt(self) -> int:
        """Get current task attempt number."""
        return max([info.get("attempt", 1) for info in self._state.changeset.values()] or [1])
    
    def save_changeset(self, task_id: str) -> None:
        """
        Save changeset to _backups/changeset_<task_id>.json.
        
        Per §9.2: Written at end of every attempt (success, failure, requeue).
        """
        changeset_path = self._vault.backups_path / f"changeset_{task_id}.json"
        
        with open(changeset_path, "w") as f:
            json.dump(self._state.changeset, f, indent=2)
    
    def load_changeset(self, task_id: str) -> dict[str, dict]:
        """
        Load changeset from _backups/changeset_<task_id>.json.
        
        Per §9.2: Accumulated across retry attempts.
        """
        changeset_path = self._vault.backups_path / f"changeset_{task_id}.json"
        
        if not changeset_path.exists():
            return {}
        
        with open(changeset_path) as f:
            return json.load(f)
    
    # =========================================================================
    # Task Logging - Per §8.10
    # =========================================================================
    
    def _log_task_completion(self, task: "Task", result: "TaskExecutionResult") -> None:
        """Log successful task completion."""
        from .logger import TaskEvent
        
        # Append metrics event
        event = TaskEvent.create(
            task_id=task.id,
            task_type=task.meta.type.value,
            turns_used=result.turns_used,
            max_turns=self._config.budgets.coding_max_turns if task.meta.type.value == "coding" else self._config.budgets.planning_max_turns,
            outcome="done",
            files_touched=len(result.actions),
            changeset_from_attempt=task.meta.attempts,
        )
        self._metrics.append(event)
    
    def _log_task_timeout(self, task: "Task", result: "TaskExecutionResult") -> None:
        """Log task timeout."""
        from .logger import TaskEvent
        
        event = TaskEvent.create(
            task_id=task.id,
            task_type=task.meta.type.value,
            turns_used=result.turns_used,
            max_turns=self._config.budgets.coding_max_turns if task.meta.type.value == "coding" else self._config.budgets.planning_max_turns,
            outcome="timeout",
            files_touched=len(result.actions),
            changeset_from_attempt=task.meta.attempts,
        )
        self._metrics.append(event)
    
    def _log_task_error(self, task: "Task", result: "TaskExecutionResult") -> None:
        """Log task error."""
        from .logger import TaskEvent
        
        event = TaskEvent.create(
            task_id=task.id,
            task_type=task.meta.type.value,
            turns_used=result.turns_used,
            max_turns=self._config.budgets.coding_max_turns if task.meta.type.value == "coding" else self._config.budgets.planning_max_turns,
            outcome="error",
            files_touched=len(result.actions),
            changeset_from_attempt=task.meta.attempts,
        )
        self._metrics.append(event)
    
    # =========================================================================
    # Output Compression - §8.9
    # =========================================================================
    
    def compress_output(self, raw: str, max_chars: int = 4000) -> str:
        """
        Compress output if it exceeds threshold.
        
        Per §8.9: Returns head(20) + tail(10) summary if too long.
        Full output is preserved in _backups/.
        """
        if len(raw) <= max_chars:
            return raw
        
        # Split into lines
        lines = raw.splitlines()
        
        if len(lines) <= 30:
            return raw  # Not worth compressing
        
        # Take head(20) + tail(10)
        head = lines[:20]
        tail = lines[-10:] if len(lines) > 10 else []
        
        summary = "\n".join(head)
        if tail:
            summary += "\n... [truncated - see _backups/ for full output]"
            summary += "\n" + "\n".join(tail)
        
        return summary
    
    def save_full_output(self, task_id: str, turn: int, output: str) -> None:
        """Save full output to _backups/execute_<task_id>_<turn>.txt."""
        output_path = self._vault.backups_path / f"execute_{task_id}_{turn}.txt"
        
        with open(output_path, "w") as f:
            f.write(output)
    
    def execute_command(
        self,
        command: str,
        cwd: Path,
        tier: str = "coding",
        timeout: int | None = None,
    ) -> "SpawnResult":
        """
        Execute a command in the sandbox.
        
        Per §8.3, 8.7, 8.12.
        """
        from .sandbox import is_command_allowed, safe_vault_path
        
        # Check command allowlist/denylist
        allowed, reason = is_command_allowed(command, tier)
        if not allowed:
            raise ValueError(f"Command not allowed: {reason}")
        
        # Check path containment in arguments
        for part in command.split():
            if '/' in part or '\\' in part or ':' in part:
                result = safe_vault_path(part, cwd)
                if result is None:
                    raise ValueError(f"Path outside allowed root: {part}")
        
        # Get timeout
        if timeout is None:
            timeout = self._config.budgets.coding_execute_timeout if tier == "coding" else 10
        
        # Execute via sandbox
        # In real impl, would use _sandbox.spawn()
        # For now, return mock result
        from .sandbox import SpawnResult
        return SpawnResult(
            exit_code=0,
            stdout="Mock output",
            stderr="",
            timed_out=False,
            overhead_ms=0,
        )
    
    def stop(self) -> None:
        """Stop the orchestrator."""
        self._state.running = False

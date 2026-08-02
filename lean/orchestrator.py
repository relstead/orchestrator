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
        """Process inbox with backoff."""
        # Check backoff
        if time.time() < self._state.inbox_backoff_until:
            return
        
        if not self._vault.inbox_path.exists():
            return
        
        content = self._vault.inbox_path.read_text().strip()
        if not content:
            return
        
        # In real impl, would call model to decompose inbox
        # For now, just archive
        self._archive_inbox_content(content)
    
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
        from .tasks import TaskState
        
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
                    
                    # In real impl, would spawn worker thread
                    # For now, just log
                    print(f"Would dispatch task {task.id} to {worker.name}")
                    
                except Exception as e:
                    print(f"Failed to dispatch task {task.id}: {e}")
    
    def _maybe_compact(self) -> None:
        """
        Maybe run compaction with backoff and cap.
        
        Per §9.4: at most one compaction per poll cycle across all projects.
        """
        if time.time() < self._state.compaction_backoff_until:
            return
        
        if not self._metrics.should_compact():
            return
        
        # Compact metrics
        count = self._metrics.compact()
        if count > 0:
            print(f"Compacted {count} events to archive")
            self._state.compaction_backoff_until = time.time() + 60
    
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

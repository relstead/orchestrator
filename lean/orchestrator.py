"""Main orchestrator - DAG + Worker Pool + Task Dispatch.

Code handles: dependency resolution, worker allocation, context building,
verification, rollback. AI handles: inference only.
"""

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .agent import Action, parse_action, build_prompt
from .config import load_config
from .dependency import DependencyGraph, build_graph_from_paths
from .indexer import Indexer
from .rollback import RollbackManager
from .sandbox import execute
from .tasks import (
    parse_task, claim_task, release_task, finish_task,
    scan_pending_tasks, scan_doing_tasks, is_stale, extract_dependencies,
)
from .verification import verify
from .vault import ensure_vault_skeleton, safe_vault_path, list_project_dirs
from .worker import WorkerPool


@dataclass
class ExecutionResult:
    """Result of task execution."""
    success: bool
    output: str
    changes: list[str]


class Orchestrator:
    """
    Main orchestrator coordinating tasks and workers.
    
    - Builds DAG from task dependencies
    - Assigns ready tasks to workers
    - Runs verification
    - Handles rollback on failure
    """
    
    def __init__(
        self,
        vault_root: Path,
        log_fn: Callable[[str], None] | None = None,
    ):
        self.vault_root = vault_root
        self.log = log_fn or (lambda x: None)
        self.config = load_config(vault_root)
        self.rollback = RollbackManager(vault_root)
        
        # Workers
        self.pool = WorkerPool(self.config.get("workers", []))
        
        # Instance ID for claiming tasks
        self.instance_id = f"{os.environ.get('COMPUTERNAME', os.environ.get('HOSTNAME', 'host'))}-{os.getpid()}"
        
        # Project index cache
        self._index_cache: dict[str, Indexer] = {}
        
        # Control
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        
        # Ensure vault structure
        ensure_vault_skeleton(vault_root)
    
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
    # Task Execution
    # =========================================================================
    
    def execute_task(self, task_path: Path) -> ExecutionResult:
        """Execute a single task with an available worker."""
        max_attempts = self.config.get("max_task_attempts", 3)
        max_turns = self.config.get("max_turns", 6)
        execute_timeout = self.config.get("execute_timeout", 30)
        
        # Parse task
        task = parse_task(task_path)
        
        # Determine project path
        project_path = task_path.parent.parent.parent / "Projects" / task_path.parent.parent.name
        if not project_path.exists():
            project_path = task_path.parent.parent.parent / "Projects" / "default"
        
        # Claim task
        claimed = claim_task(task_path, self.instance_id)
        if not claimed:
            return ExecutionResult(success=False, output="Failed to claim task", changes=[])
        
        task_path = claimed
        
        # Update attempts - re-parse after claim to get fresh state
        from .tasks import set_meta
        task = parse_task(task_path)
        task.attempts += 1
        new_txt = set_meta(task.raw_text, task.task_type, task.attempts)
        task_path.write_text(new_txt)
        
        self.log(f"Executing: {task.body[:50]}... (attempt {task.attempts}/{max_attempts})")
        
        # Snapshot before execution
        snapshot_path = self.rollback.snapshot(project_path, f"task-{task_path.stem[:8]}")
        
        try:
            # Build context
            relevant_context = self._get_relevant_context(project_path, task.body)
            prompt = build_prompt(task.body, relevant_context)
            
            # Execute agent loop
            success, output, changes = self._agent_loop(
                prompt=prompt,
                project_path=project_path,
                max_turns=max_turns,
                execute_timeout=execute_timeout,
                task_id=task_path.stem,
            )
            
            # Verification
            if success:
                result = verify(project_path)
                success = result.passed
                if not result.passed:
                    output = f"Verification failed:\n{result.output}"
            
            # Handle failure
            if not success:
                if snapshot_path:
                    self.rollback.rollback(snapshot_path, project_path)
                
                if task.attempts >= max_attempts:
                    finish_task(task_path, success=False, result=output[:200])
                    self.log(f"Failed after {max_attempts} attempts")
                    return ExecutionResult(success=False, output=output, changes=changes)
                
                # Release back to pending for retry
                release_task(task_path, back_to_pending=True)
                return ExecutionResult(success=False, output=output, changes=changes)
            
            # Success
            finish_task(task_path, success=True, result=output[:200])
            self.log(f"Completed: {task.body[:50]}...")
            return ExecutionResult(success=True, output=output, changes=changes)
        
        except Exception as e:
            self.log(f"Error: {e}")
            if snapshot_path:
                self.rollback.rollback(snapshot_path, project_path)
            finish_task(task_path, success=False, result=str(e))
            return ExecutionResult(success=False, output=str(e), changes=[])
    
    def _agent_loop(
        self,
        prompt: str,
        project_path: Path,
        max_turns: int,
        execute_timeout: int,
        task_id: str | None = None,
    ) -> tuple[bool, str, list[str]]:
        """Run the AI agent loop."""
        messages = [{"role": "system", "content": prompt}]
        changed_files: list[str] = []
        read_files: set[str] = set()
        
        for turn in range(max_turns):
            # Call AI
            try:
                worker, response = self.pool.call("general", messages)
            except RuntimeError as e:
                return False, f"No workers available: {e}", changed_files
            
            # Persist turn progress (CRASH RESILIENCE per SPEC.md)
            self.persist_turn_progress(task_id, turn, messages, response)
            
            # Parse action
            action = parse_action(response)
            if not action:
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": "Invalid response. Return JSON with action field."})
                continue
            
            # Execute action
            obs = self._execute_action(action, project_path, execute_timeout, read_files, changed_files)
            
            # Persist after action too
            self.persist_turn_progress(task_id, turn, messages, obs, is_observation=True)
            
            # Check for final
            if action.action == "final":
                return True, action.result or obs, changed_files
            
            # Continue
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": obs})
        
        return False, f"Hit {max_turns} turn limit", changed_files
    
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
        
        Persists the conversation transcript so that if the process crashes,
        progress can be recovered. Files are written to _backups/ with a
        naming convention that allows identification and resumption.
        """
        if task_id is None:
            return
        
        backup_dir = self.vault_root / "_backups"
        backup_dir.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        role = "obs" if is_observation else "turn"
        progress_file = backup_dir / f"progress_{task_id}_{turn}_{role}_{timestamp}.json"
        
        data = {
            "task_id": task_id,
            "turn": turn,
            "is_observation": is_observation,
            "timestamp": timestamp,
            "messages": messages,
            "last_response": last_response,
        }
        
        try:
            progress_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass  # Don't fail on persistence errors
    
    def _execute_action(
        self,
        action: Action,
        project_path: Path,
        timeout: int,
        read_files: set[str],
        changed_files: list[str],
    ) -> str:
        """Execute a single action."""
        
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
            read_files.add(action.path)
            return target.read_text(encoding="utf-8", errors="replace")[:3000]
        
        elif action.action == "write":
            if not action.path:
                return "ERROR: no path"
            
            target = safe_vault_path(project_path, action.path)
            if target.exists() and action.path not in read_files:
                return f"REFUSED: overwrite without read first"
            
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action.content or "", encoding="utf-8")
            changed_files.append(action.path)
            return f"OK: wrote {len(action.content or '')} chars"
        
        elif action.action == "execute":
            if not action.command:
                return "ERROR: no command"
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
        while not self._stop.is_set():
            # Recover stale tasks
            self._sweep_stale_claims(max_age_minutes=self.config.get("stale_claim_minutes", 30))
            
            # Scan pending tasks and build graph
            pending_paths = scan_pending_tasks(self.vault_root)
            graph = build_graph_from_paths(pending_paths)
            
            # Check for cycles
            if graph.detect_cycle():
                self.log("WARNING: Dependency cycle detected")
            
            # Get ready tasks
            ready = graph.get_ready()
            
            # Dispatch ready tasks to idle workers (parallel execution)
            for task_node in ready:
                worker = self.pool.get_idle()
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
        try:
            result = self.execute_task(task_path)
            success = result.success
        except Exception:
            success = False
        finally:
            worker.finish_job(success=success)

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
from .vault import ensure_vault_skeleton, safe_vault_path, list_project_dirs, ensure_project_skeleton, new_task_file
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
                
                # Archive this line with timestamp and project
                timestamp = datetime.now().isoformat()
                archive_path.write_text(
                    f"\n[{timestamp}] [{project}] {task_body}",
                    encoding="utf-8",
                )
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
        Add an entry to _digest.md with timestamp.
        
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
        
        # Read existing digest
        existing = ""
        if digest_path.exists():
            existing = digest_path.read_text(encoding="utf-8")
        
        # Append new entry
        new_entry = f"\n[{timestamp}] {icon} {message}"
        
        # Keep only last 100 entries (compaction)
        lines = existing.strip().split("\n") if existing.strip() else []
        if len(lines) > 100:
            lines = lines[-100:]
        
        digest_path.write_text("\n".join(lines) + new_entry + "\n", encoding="utf-8")
    
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
        """Add an entry to a project's STATUS.md."""
        status_path = self.vault_root / "Projects" / project_name / "STATUS.md"
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n[{timestamp}] {message}"
        
        existing = ""
        if status_path.exists():
            existing = status_path.read_text(encoding="utf-8")
        
        status_path.write_text(existing + entry + "\n", encoding="utf-8")
    
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
        # Track cycle count for periodic tasks
        cycle_count = 0
        
        while not self._stop.is_set():
            cycle_count += 1
            
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
        task_name = task_path.stem
        try:
            result = self.execute_task(task_path)
            success = result.success
            
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
            worker.finish_job(success=success)

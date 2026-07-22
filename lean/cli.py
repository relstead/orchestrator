"""CLI for vault orchestrator."""

import json
import sys
import time
from pathlib import Path


def init(path: str) -> None:
    """Initialize a new vault."""
    vault = Path(path).expanduser().absolute()
    vault.mkdir(parents=True, exist_ok=True)
    
    # Create structure matching SPEC.md and ensure_vault_skeleton
    (vault / "Projects").mkdir(exist_ok=True)
    (vault / "_backups").mkdir(exist_ok=True)
    (vault / "_archive").mkdir(exist_ok=True)
    (vault / "_archive/_digest").mkdir(exist_ok=True)
    (vault / "Skills").mkdir(exist_ok=True)
    (vault / "_logs").mkdir(exist_ok=True)
    
    # Default config with task timeout
    config = {
        "max_task_attempts": 3,
        "max_turns": 6,
        "execute_timeout": 30,
        "stale_claim_minutes": 30,
        "task_timeout_seconds": 300,
        "workers": [
            {"name": "default", "model": "auto", "base_url": "https://api.openai.com/v1", "api_key": ""}
        ],
    }
    (vault / "config.json").write_text(json.dumps(config, indent=2))
    
    # Vault files
    (vault / "_active.md").write_text("default")
    (vault / "_inbox.md").write_text("")
    (vault / "_inbox_archive.md").write_text("")
    (vault / "_digest.md").write_text("")
    (vault / "_audit.md").write_text("")
    (vault / "_costs.json").write_text(json.dumps({"total_usd": 0.0, "by_project": {}, "by_model": {}}))
    
    # Default project
    default_proj = vault / "Projects" / "default"
    (default_proj / "assets").mkdir()
    (default_proj / "NOTES.md").write_text("")
    (default_proj / "STATUS.md").write_text("")
    for sub in ("tasks/pending", "tasks/doing", "tasks/done", "tasks/blocked", "tasks/waiting", "tasks/failed"):
        (default_proj / sub).mkdir(parents=True, exist_ok=True)
    # Project archive directory
    (vault / "_archive" / "default").mkdir(exist_ok=True)
    
    print(f"Vault initialized at: {vault}")
    print("Edit config.json to add your API keys and workers.")
    print("Use ${ENV_VAR} syntax for secret management (e.g., \"api_key\": \"${MY_API_KEY}\")")


def run(path: str) -> None:
    """Run the orchestrator."""
    vault = Path(path).expanduser().absolute()
    if not vault.exists():
        print(f"Error: vault not found at {vault}")
        print("Run 'init' first.")
        sys.exit(1)
    
    from .orchestrator import Orchestrator
    
    orch = Orchestrator(vault, log_fn=print)
    orch.start()
    
    print("Orchestrator running. Press Ctrl+C to stop.")
    
    try:
        while True:
            time.sleep(5)
            pending = len(list(vault.rglob("pending/**/*.md")))
            doing = len(list(vault.rglob("doing/**/*.md")))
            done = len(list(vault.rglob("done/**/*.md")))
            failed = len(list(vault.rglob("failed/**/*.md")))
            
            # Show cost summary if available
            costs_path = vault / "_costs.json"
            cost_info = ""
            if costs_path.exists():
                try:
                    costs = json.loads(costs_path.read_text())
                    if costs.get("total_usd", 0) > 0:
                        cost_info = f" | ${costs['total_usd']:.4f} total"
                except Exception:
                    pass
            
            print(f"[{time.strftime('%H:%M:%S')}] Tasks: {pending} pending, {doing} running, {done} done, {failed} failed{cost_info}")
    except KeyboardInterrupt:
        print("\nStopping...")
        orch.stop()


def status(path: str) -> None:
    """Show vault status."""
    vault = Path(path).expanduser().absolute()
    
    pending = len(list(vault.rglob("pending/**/*.md")))
    doing = len(list(vault.rglob("doing/**/*.md")))
    done = len(list(vault.rglob("done/**/*.md")))
    failed = len(list(vault.rglob("failed/**/*.md")))
    waiting = len(list(vault.rglob("waiting/**/*.md")))
    
    print(f"Vault: {vault}")
    print(f"Tasks:")
    print(f"  Pending: {pending}")
    print(f"  Running: {doing}")
    print(f"  Done: {done}")
    print(f"  Failed: {failed}")
    print(f"  Waiting: {waiting}")
    
    # Show cost summary
    costs_path = vault / "_costs.json"
    if costs_path.exists():
        try:
            costs = json.loads(costs_path.read_text())
            print(f"\nCosts:")
            print(f"  Total: ${costs.get('total_usd', 0):.4f}")
            if costs.get("by_project"):
                print(f"  By project:")
                for proj, amt in sorted(costs["by_project"].items()):
                    print(f"    {proj}: ${amt:.4f}")
            if costs.get("by_model"):
                print(f"  By model:")
                for model, amt in sorted(costs["by_model"].items()):
                    print(f"    {model}: ${amt:.4f}")
        except Exception:
            pass


def logs(path: str, lines: int = 50) -> None:
    """Show recent log entries."""
    vault = Path(path).expanduser().absolute()
    log_file = vault / "_logs" / "orchestrator.jsonl"
    
    if not log_file.exists():
        print("No logs found. Run 'lean run' first.")
        return
    
    content = log_file.read_text(encoding="utf-8")
    log_lines = content.strip().split("\n")
    
    # Show last N lines
    for line in log_lines[-lines:]:
        if line.strip():
            try:
                # Try to parse as JSON for pretty printing
                entry = json.loads(line)
                ts = entry.get("timestamp", "")[11:19]  # Just time portion
                level = entry.get("level", "info").upper()
                msg = entry.get("message", "")
                extra = {k: v for k, v in entry.items() 
                        if k not in ("timestamp", "level", "message")}
                extra_str = " ".join(f"{k}={v}" for k, v in extra.items()) if extra else ""
                if extra_str:
                    print(f"[{ts}] [{level:8}] {msg} ({extra_str})")
                else:
                    print(f"[{ts}] [{level:8}] {msg}")
            except json.JSONDecodeError:
                # Plain text line
                print(line)


def pause(path: str) -> None:
    """Pause task processing."""
    vault = Path(path).expanduser().absolute()
    
    # Check for a running orchestrator (via PID file)
    pid_file = vault / ".orchestrator.pid"
    if not pid_file.exists():
        print("No orchestrator running. Run 'lean run' first.")
        return
    
    # For now, just show a message - real pause would need IPC
    print("Note: Pause/resume requires the orchestrator to be running.")
    print("The orchestrator process can be paused with Ctrl+Z and resumed with 'fg'.")


def task(path: str, task_id: str) -> None:
    """Show details of a specific task."""
    vault = Path(path).expanduser().absolute()
    
    # Find the task file
    task_file = None
    for folder in ("pending", "doing", "done", "failed", "waiting", "blocked"):
        candidates = list(vault.rglob(f"**/{folder}/{task_id}.md"))
        if candidates:
            task_file = candidates[0]
            status = folder
            break
    
    if not task_file:
        print(f"Task '{task_id}' not found.")
        return
    
    print(f"Task: {task_id}")
    print(f"Status: {status}")
    print(f"Path: {task_file}")
    
    content = task_file.read_text(encoding="utf-8")
    print(f"\n--- Content ---")
    print(content[:500])
    if len(content) > 500:
        print(f"\n... ({len(content) - 500} more characters)")


def add(path: str, task: str, project: str = "default", task_type: str = "general") -> None:
    """Add a task."""
    vault = Path(path).expanduser().absolute()
    
    from .vault import ensure_project_skeleton, new_task_file
    project_dir = ensure_project_skeleton(vault, project)
    
    task_path = new_task_file(project_dir, task_type, task)
    print(f"Task added: {task_path} (type={task_type})")


def main() -> None:
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage:")
        print("  lean init <path>")
        print("  lean run <path>")
        print("  lean status <path>")
        print("  lean add <path> <task> [project] [type]")
        print("  lean logs <path> [lines]")
        print("  lean pause <path>")
        print("  lean task <path> <task-id>")
        print("")
        print("Task types: general, coding, reasoning")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "init":
        if len(sys.argv) < 3:
            print("Usage: lean init <path>")
            sys.exit(1)
        init(sys.argv[2])
    
    elif cmd == "run":
        if len(sys.argv) < 3:
            print("Usage: lean run <path>")
            sys.exit(1)
        run(sys.argv[2])
    
    elif cmd == "status":
        if len(sys.argv) < 3:
            print("Usage: lean status <path>")
            sys.exit(1)
        status(sys.argv[2])
    
    elif cmd == "add":
        if len(sys.argv) < 4:
            print("Usage: lean add <path> <task> [project] [type]")
            sys.exit(1)
        project = sys.argv[4] if len(sys.argv) > 4 else "default"
        task_type = sys.argv[5] if len(sys.argv) > 5 else "general"
        add(sys.argv[2], sys.argv[3], project, task_type)
    
    elif cmd == "logs":
        if len(sys.argv) < 3:
            print("Usage: lean logs <path> [lines]")
            sys.exit(1)
        lines = int(sys.argv[3]) if len(sys.argv) > 3 else 50
        logs(sys.argv[2], lines)
    
    elif cmd == "pause":
        if len(sys.argv) < 3:
            print("Usage: lean pause <path>")
            sys.exit(1)
        pause(sys.argv[2])
    
    elif cmd == "task":
        if len(sys.argv) < 4:
            print("Usage: lean task <path> <task-id>")
            sys.exit(1)
        task(sys.argv[2], sys.argv[3])
    
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()

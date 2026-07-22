"""CLI for vault orchestrator."""

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
    
    # Default config
    import json
    config = {
        "max_task_attempts": 3,
        "max_turns": 6,
        "execute_timeout": 30,
        "stale_claim_minutes": 30,
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
    
    # Default project
    default_proj = vault / "Projects" / "default"
    (default_proj / "assets").mkdir()
    (default_proj / "NOTES.md").write_text("")
    (default_proj / "STATUS.md").write_text("")
    for sub in ("tasks/pending", "tasks/doing", "tasks/done", "tasks/blocked", "tasks/waiting"):
        (default_proj / sub).mkdir(parents=True, exist_ok=True)
    # Project archive directory
    (vault / "_archive" / "default").mkdir(exist_ok=True)
    
    print(f"Vault initialized at: {vault}")
    print("Edit config.json to add your API keys and workers.")


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
            print(f"[{time.strftime('%H:%M:%S')}] Tasks: {pending} pending, {doing} running, {done} done, {failed} failed")
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
    
    print(f"Vault: {vault}")
    print(f"Tasks:")
    print(f"  Pending: {pending}")
    print(f"  Running: {doing}")
    print(f"  Done: {done}")
    print(f"  Failed: {failed}")


def add(path: str, task: str, project: str = "default") -> None:
    """Add a task."""
    vault = Path(path).expanduser().absolute()
    
    from .vault import ensure_project_skeleton, new_task_file
    project_dir = ensure_project_skeleton(vault, project)
    
    task_path = new_task_file(project_dir, "general", task)
    print(f"Task added: {task_path}")


def main() -> None:
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage:")
        print("  lean init <path>")
        print("  lean run <path>")
        print("  lean status <path>")
        print("  lean add <path> <task> [project]")
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
            print("Usage: lean add <path> <task> [project]")
            sys.exit(1)
        project = sys.argv[4] if len(sys.argv) > 4 else "default"
        add(sys.argv[2], sys.argv[3], project)
    
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()

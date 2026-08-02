"""
CLI module for Vault Orchestrator.

Command-line interface for the lean tool.
Per §4.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def init_vault(vault_path: Path) -> int:
    """Initialize a new vault."""
    from .vault import open_vault
    from .config import Config
    
    if vault_path.exists() and any(vault_path.iterdir()):
        print(f"Error: {vault_path} is not empty", file=sys.stderr)
        return 1
    
    # Create vault
    vault = open_vault(vault_path)
    
    # Create default config
    config_path = vault_path / "config.json"
    config = Config()
    config.save(config_path)
    
    print(f"Initialized vault at {vault_path}")
    print(f"Created config at {config_path}")
    
    return 0


def run_vault(vault_path: Path) -> int:
    """Run the orchestrator."""
    from .orchestrator import Orchestrator
    from .config import Config
    
    # Load config
    config_path = vault_path / "config.json"
    if config_path.exists():
        config = Config.from_file(config_path)
    else:
        config = Config()
    
    # Create and run orchestrator
    orchestrator = Orchestrator(vault_path, config)
    
    try:
        orchestrator.run()
    except KeyboardInterrupt:
        pass
    
    return 0


def status_vault(vault_path: Path) -> int:
    """Show vault status."""
    from .vault import Vault
    from .tasks import TaskStore, TaskState
    
    vault = Vault(vault_path)
    task_store = TaskStore(vault)
    
    print(f"Vault: {vault_path}")
    print()
    
    # Discover projects
    projects = vault.discover_projects()
    if not projects:
        print("No projects found.")
        return 0
    
    for project in projects:
        print(f"Project: {project}")
        
        # Count tasks by state
        counts = {state: 0 for state in TaskState}
        for state in TaskState:
            tasks = task_store.get_tasks_in_state(project, state)
            counts[state] = len(tasks)
        
        print(f"  pending: {counts[TaskState.PENDING]}")
        print(f"  doing:   {counts[TaskState.DOING]}")
        print(f"  done:    {counts[TaskState.DONE]}")
        print(f"  blocked: {counts[TaskState.BLOCKED]}")
        print(f"  waiting: {counts[TaskState.WAITING]}")
        print(f"  failed:  {counts[TaskState.FAILED]}")
        print()
    
    return 0


def add_task(vault_path: Path, task_text: str, project: str | None = None) -> int:
    """Add a task to the inbox or a project."""
    from .vault import Vault
    from .tasks import TaskStore, TaskType
    
    vault = Vault(vault_path)
    task_store = TaskStore(vault)
    
    if project:
        # Add to specific project
        vault.ensure_project_skeleton(project)
        task = task_store.create_task(
            project=project,
            title=task_text[:50],
            body=task_text,
            task_type=TaskType.CODING,
        )
        print(f"Created task {task.id} in project {project}")
    else:
        # Add to inbox
        inbox = vault_path / "_inbox.md"
        with open(inbox, "a") as f:
            if inbox.exists() and inbox.read_text().strip():
                f.write("\n")
            f.write(task_text)
        print(f"Added to inbox: {task_text[:50]}")
    
    return 0


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Vault Orchestrator - AI-powered task management",
        prog="lean",
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # init command
    init_parser = subparsers.add_parser("init", help="Initialize a new vault")
    init_parser.add_argument("vault-path", type=Path, help="Path to vault")
    
    # run command
    run_parser = subparsers.add_parser("run", help="Run the orchestrator")
    run_parser.add_argument("vault-path", type=Path, help="Path to vault")
    
    # status command
    status_parser = subparsers.add_parser("status", help="Show vault status")
    status_parser.add_argument("vault-path", type=Path, help="Path to vault")
    
    # add command
    add_parser = subparsers.add_parser("add", help="Add a task")
    add_parser.add_argument("vault-path", type=Path, help="Path to vault")
    add_parser.add_argument("task", help="Task description")
    add_parser.add_argument("project", nargs="?", help="Project name (optional)")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    vault_path = getattr(args, "vault_path", None)
    
    if args.command == "init":
        return init_vault(Path(args.vault_path))
    elif args.command == "run":
        return run_vault(Path(args.vault_path))
    elif args.command == "status":
        return status_vault(Path(args.vault_path))
    elif args.command == "add":
        return add_task(Path(args.vault_path), args.task, args.project)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Vault filesystem access and operations.

Security-critical: path containment ensures AI never accesses files outside vault.
"""

import re
from datetime import datetime
from pathlib import Path


class VaultAccessError(Exception):
    """Raised when an operation attempts to access paths outside the vault."""
    pass


PROJECT_NAME_RE = re.compile(r"[^A-Za-z0-9 _\-]")


def safe_vault_path(vault_root: Path, relative: str) -> Path:
    """
    Resolve a path relative to the vault root, rejecting paths that escape.
    
    This is the ONLY way to access files - all read/write/list goes through here.
    """
    vault_root_resolved = vault_root.resolve()
    candidate = (vault_root_resolved / relative).resolve()
    if candidate != vault_root_resolved and vault_root_resolved not in candidate.parents:
        raise VaultAccessError(f"Refused path outside vault: {relative}")
    return candidate


def safe_project_path(project_path: Path, relative: str) -> Path:
    """
    Resolve a path relative to the project, rejecting paths that escape.
    
    Used for write actions that should be confined to the project directory.
    """
    project_resolved = project_path.resolve()
    candidate = (project_resolved / relative).resolve()
    if project_resolved not in candidate.parents and candidate != project_resolved:
        raise VaultAccessError(f"Refused path outside project: {relative}")
    return candidate


def sanitize_project_name(name: str) -> str:
    """Remove invalid characters from a project name."""
    name = PROJECT_NAME_RE.sub("", name).strip()
    return name[:60] if name else "default"


def ensure_vault_skeleton(vault_root: Path) -> None:
    """Create the vault skeleton structure if it doesn't exist."""
    vault_root.mkdir(parents=True, exist_ok=True)
    for f in ("_active.md", "_inbox.md", "_inbox_archive.md", "_digest.md"):
        p = safe_vault_path(vault_root, f)
        if not p.exists():
            p.write_text("", encoding="utf-8")
    safe_vault_path(vault_root, "Projects").mkdir(exist_ok=True)
    safe_vault_path(vault_root, "_backups").mkdir(exist_ok=True)
    safe_vault_path(vault_root, "_archive").mkdir(exist_ok=True)
    safe_vault_path(vault_root, "_archive/_digest").mkdir(exist_ok=True)
    safe_vault_path(vault_root, "Skills").mkdir(exist_ok=True)


def ensure_project_skeleton(vault_root: Path, project_name: str) -> Path:
    """Create a project skeleton structure if it doesn't exist."""
    project_dir = safe_vault_path(vault_root, f"Projects/{project_name}")
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "assets").mkdir(exist_ok=True)
    for sub in ("tasks/pending", "tasks/doing", "tasks/done", "tasks/blocked", "tasks/waiting"):
        (project_dir / sub).mkdir(parents=True, exist_ok=True)
    for f in ("NOTES.md", "STATUS.md"):
        p = project_dir / f
        if not p.exists():
            p.write_text("", encoding="utf-8")
    # Create project archive directory per SPEC.md
    archive_dir = safe_vault_path(vault_root, f"_archive/{project_name}")
    archive_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


def list_project_dirs(vault_root: Path) -> list[Path]:
    """List all project directories in the vault."""
    projects_root = safe_vault_path(vault_root, "Projects")
    if not projects_root.exists():
        return []
    return sorted([d for d in projects_root.iterdir() if d.is_dir()])


def get_active_project(vault_root: Path) -> str | None:
    """Get the name of the currently active project."""
    p = safe_vault_path(vault_root, "_active.md")
    if p.exists():
        return p.read_text().strip()
    return None


def set_active_project(vault_root: Path, project_name: str) -> None:
    """Set the currently active project."""
    p = safe_vault_path(vault_root, "_active.md")
    p.write_text(project_name, encoding="utf-8")


def new_task_file(project_dir: Path, task_type: str, text: str) -> Path:
    """Create a new task file in the pending folder."""
    pending = project_dir / "tasks" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S%f")
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())[:40].strip("-") or "task"
    path = pending / f"{stamp}-{slug}.md"
    header = f"<!-- meta: type={task_type} attempts=0 -->\n\n"
    path.write_text(header + text.strip() + "\n", encoding="utf-8")
    return path

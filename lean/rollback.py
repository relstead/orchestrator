"""Snapshot and rollback for safe execution.

Creates full project snapshots before execution,
restores on failure.
"""

import shutil
from datetime import datetime
from pathlib import Path


class RollbackManager:
    """Manages snapshots and rollback for projects."""
    
    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.backup_dir = vault_root / "_backups"
        self.backup_dir.mkdir(exist_ok=True)

    def snapshot(self, project_path: Path, label: str = "") -> Path | None:
        """Create a snapshot of the project."""
        if not project_path.exists():
            return None
        
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{project_path.name}__{label}__{timestamp}" if label else f"{project_path.name}__{timestamp}"
        snapshot_path = self.backup_dir / name
        
        try:
            shutil.copytree(
                project_path,
                snapshot_path,
                ignore=shutil.ignore_patterns("tasks", "__pycache__", ".git", "node_modules"),
            )
            return snapshot_path
        except Exception:
            return None

    def rollback(self, snapshot_path: Path, project_path: Path) -> bool:
        """Restore project from snapshot."""
        if not snapshot_path.exists():
            return False
        
        try:
            if project_path.exists():
                shutil.rmtree(project_path)
            shutil.copytree(
                snapshot_path,
                project_path,
                ignore=shutil.ignore_patterns("tasks", "__pycache__"),
            )
            return True
        except Exception:
            return False

    def list_snapshots(self, project_name: str) -> list[dict]:
        """List snapshots for a project."""
        snapshots = []
        prefix = f"{project_name}__"
        
        for p in self.backup_dir.iterdir():
            if p.is_dir() and p.name.startswith(prefix):
                parts = p.name.split("__")
                timestamp = parts[-1] if len(parts) > 1 else "unknown"
                snapshots.append({"path": str(p), "timestamp": timestamp})
        
        return sorted(snapshots, key=lambda x: x["timestamp"], reverse=True)

    def cleanup_old(self, project_name: str, keep: int = 5) -> int:
        """Remove old snapshots, keeping most recent N."""
        snapshots = self.list_snapshots(project_name)
        removed = 0
        
        for snap in snapshots[keep:]:
            try:
                shutil.rmtree(Path(snap["path"]))
                removed += 1
            except Exception:
                pass
        
        return removed

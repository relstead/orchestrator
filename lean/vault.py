"""
Vault module for Vault Orchestrator.

Handles vault path management, skeleton creation, and path containment validation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


class VaultError(Exception):
    """Base exception for vault operations."""
    pass


class Vault:
    """
    Represents a vault and its directory structure.
    
    Vault Schema (§3):
        <vault root>/
          _active.md
          _inbox.md
          _inbox_archive.md
          _digest.md
          _backups/
          _archive/<project>/
          _archive/_digest/
          _archive/_metrics/
          _metrics/
          Skills/<name>/SKILL.md
          Projects/<name>/
            assets/
            NOTES.md
            STATUS.md
            tasks/
              pending/ doing/ done/ blocked/ waiting/ failed/
    """
    
    def __init__(self, root: Path):
        self._root = root.resolve()
    
    @property
    def root(self) -> Path:
        """Get the vault root path."""
        return self._root
    
    @property
    def config_path(self) -> Path:
        """Get the config file path."""
        return self._root / "config.json"
    
    @property
    def active_path(self) -> Path:
        """Get the active project marker path."""
        return self._root / "_active.md"
    
    @property
    def inbox_path(self) -> Path:
        """Get the inbox path."""
        return self._root / "_inbox.md"
    
    @property
    def inbox_archive_path(self) -> Path:
        """Get the inbox archive path."""
        return self._root / "_inbox_archive.md"
    
    @property
    def digest_path(self) -> Path:
        """Get the digest path."""
        return self._root / "_digest.md"
    
    @property
    def backups_path(self) -> Path:
        """Get the backups directory path."""
        return self._root / "_backups"
    
    @property
    def archive_path(self) -> Path:
        """Get the archive directory path."""
        return self._root / "_archive"
    
    @property
    def archive_digest_path(self) -> Path:
        """Get the digest archive path."""
        return self._root / "_archive" / "_digest"
    
    @property
    def archive_metrics_path(self) -> Path:
        """Get the metrics archive path."""
        return self._root / "_archive" / "_metrics"
    
    @property
    def metrics_path(self) -> Path:
        """Get the metrics directory path."""
        return self._root / "_metrics"
    
    @property
    def events_path(self) -> Path:
        """Get the events log path."""
        return self._metrics / "events.jsonl"
    
    @property
    def skills_path(self) -> Path:
        """Get the skills directory path."""
        return self._root / "Skills"
    
    @property
    def projects_path(self) -> Path:
        """Get the projects directory path."""
        return self._root / "Projects"
    
    @property
    def active_project(self) -> str | None:
        """Get the currently active project name."""
        if not self.active_path.exists():
            return None
        
        content = self.active_path.read_text().strip()
        # Extract project name from first line or content
        lines = [l.strip() for l in content.split('\n') if l.strip()]
        if lines:
            return lines[0]
        return None
    
    def set_active_project(self, project: str) -> None:
        """Set the active project."""
        self.active_path.write_text(project)
    
    def get_project_path(self, name: str) -> Path:
        """Get a project directory path."""
        return self._root / "Projects" / name
    
    def ensure_skeleton(self) -> None:
        """
        Ensure the vault has the required directory structure.
        
        Creates any missing directories and files per §3 schema.
        """
        # Ensure directories
        self.backups_path.mkdir(parents=True, exist_ok=True)
        self.archive_path.mkdir(parents=True, exist_ok=True)
        self.archive_digest_path.mkdir(parents=True, exist_ok=True)
        self.archive_metrics_path.mkdir(parents=True, exist_ok=True)
        self.metrics_path.mkdir(parents=True, exist_ok=True)
        self.skills_path.mkdir(parents=True, exist_ok=True)
        self.projects_path.mkdir(parents=True, exist_ok=True)
        
        # Ensure empty files exist
        for path in [self.inbox_path, self.digest_path]:
            if not path.exists():
                path.write_text("")
    
    def discover_projects(self) -> list[str]:
        """
        Discover all projects in the vault.
        
        A project is a directory under Projects/ that has the required
        task subdirectories.
        """
        projects = []
        
        if not self.projects_path.exists():
            return projects
        
        for item in self.projects_path.iterdir():
            if item.is_dir():
                # Check if it's a valid project (has tasks subdirectory)
                if (item / "tasks").is_dir():
                    projects.append(item.name)
        
        return sorted(projects)
    
    def ensure_project_skeleton(self, name: str) -> Path:
        """
        Ensure a project has the required directory structure.
        
        Creates the project directory with:
            - assets/
            - NOTES.md
            - STATUS.md
            - tasks/pending/
            - tasks/doing/
            - tasks/done/
            - tasks/blocked/
            - tasks/waiting/
            - tasks/failed/
        
        Returns the project path.
        """
        project_path = self.get_project_path(name)
        
        # Create directories
        (project_path / "assets").mkdir(parents=True, exist_ok=True)
        (project_path / "tasks" / "pending").mkdir(parents=True, exist_ok=True)
        (project_path / "tasks" / "doing").mkdir(parents=True, exist_ok=True)
        (project_path / "tasks" / "done").mkdir(parents=True, exist_ok=True)
        (project_path / "tasks" / "blocked").mkdir(parents=True, exist_ok=True)
        (project_path / "tasks" / "waiting").mkdir(parents=True, exist_ok=True)
        (project_path / "tasks" / "failed").mkdir(parents=True, exist_ok=True)
        
        # Create empty files
        for fname in ["NOTES.md", "STATUS.md"]:
            fpath = project_path / fname
            if not fpath.exists():
                fpath.write_text("")
        
        return project_path
    
    def contains_path(self, path: Path) -> bool:
        """
        Check if a path is contained within the vault.
        
        This is the containment check used for path safety.
        """
        try:
            path.resolve().relative_to(self._root)
            return True
        except ValueError:
            return False
    
    def validate_path(self, path: Path) -> Path | None:
        """
        Validate and return a path if it's within the vault.
        
        Returns None if the path escapes the vault.
        """
        if not self.contains_path(path):
            return None
        return path.resolve()


def open_vault(path: Path) -> Vault:
    """
    Open or create a vault at the given path.
    
    Creates the vault skeleton if it doesn't exist.
    """
    vault = Vault(path)
    vault.ensure_skeleton()
    return vault

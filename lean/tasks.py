"""Task file handling and lifecycle.

Task state is represented by folder location:
- pending/ → doing/ → done/ or failed/ or blocked/ or waiting/
"""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


APP_CLAIM_MARK = "<!-- app-claim:"
META_RE = re.compile(r"<!--\s*meta:\s*type=(\w+)\s+attempts=(\d+)\s*-->")
CLAIM_RE = re.compile(r"<!--\s*app-claim:\s*(.+?)\s*-->")


@dataclass
class TaskFile:
    """Parsed task file."""
    path: Path
    task_type: str
    attempts: int
    body: str
    raw_text: str


def parse_task(path: Path) -> TaskFile:
    """Parse a task file and extract metadata."""
    txt = path.read_text(encoding="utf-8")
    m = META_RE.search(txt)
    task_type = m.group(1) if m else "general"
    attempts = int(m.group(2)) if m else 0
    body = META_RE.sub("", txt)
    body = CLAIM_RE.sub("", body).strip()
    return TaskFile(path=path, task_type=task_type, attempts=attempts, body=body, raw_text=txt)


def set_meta(txt: str, task_type: str, attempts: int) -> str:
    """Update the meta line in a task file."""
    new_meta = f"<!-- meta: type={task_type} attempts={attempts} -->"
    if META_RE.search(txt):
        return META_RE.sub(new_meta, txt)
    return new_meta + "\n\n" + txt


def extract_claim_line(txt: str) -> str | None:
    """Return the literal '<!-- app-claim: ... -->' line if present."""
    m = CLAIM_RE.search(txt)
    return m.group(0) if m else None


def claim_task(task_path: Path, instance_id: str) -> Path | None:
    """
    Move a task from pending to doing, marking it as claimed.
    
    Returns path to claimed task, or None if claim failed.
    """
    doing_dir = task_path.parent.parent / "doing"
    doing_dir.mkdir(parents=True, exist_ok=True)
    dest = doing_dir / task_path.name
    try:
        import os
        os.replace(task_path, dest)
    except (FileNotFoundError, PermissionError):
        return None
    
    # Add claim marker
    txt = dest.read_text(encoding="utf-8")
    stamp = datetime.now().isoformat(timespec="seconds")
    txt = f"<!-- app-claim: {instance_id} @ {stamp} -->\n" + txt
    dest.write_text(txt, encoding="utf-8")
    return dest


def release_task(task_path: Path, back_to_pending: bool = True) -> None:
    """Release a task from doing, either back to pending or to another folder."""
    if not task_path.exists():
        return
    
    dest_folder = task_path.parent.parent / ("pending" if back_to_pending else "waiting")
    dest_folder.mkdir(parents=True, exist_ok=True)
    dest = dest_folder / task_path.name
    
    import os
    os.replace(task_path, dest)


def finish_task(task_path: Path, success: bool, result: str = "") -> None:
    """Move task to done or failed, with result."""
    if not task_path.exists():
        return
    
    dest_folder = task_path.parent.parent / ("done" if success else "failed")
    dest_folder.mkdir(parents=True, exist_ok=True)
    dest = dest_folder / task_path.name
    
    # Append result
    txt = task_path.read_text(encoding="utf-8")
    if result:
        txt += f"\n\n## Result\n{result}\n"
    
    txt += f"\n## Completed\n{datetime.now().isoformat()}\n"
    dest.write_text(txt, encoding="utf-8")
    
    import os
    os.replace(task_path, dest)


def scan_pending_tasks(vault_root: Path) -> list[Path]:
    """Scan all pending tasks across all projects."""
    pending = vault_root / "pending"
    if not pending.exists():
        return []
    
    tasks = []
    for project_dir in pending.iterdir():
        if not project_dir.is_dir():
            continue
        tasks_dir = project_dir / "tasks" / "pending"
        if tasks_dir.exists():
            tasks.extend(tasks_dir.glob("*.md"))
    return tasks


def scan_doing_tasks(vault_root: Path) -> list[Path]:
    """Scan all tasks currently being worked on."""
    doing = vault_root / "doing"
    if not doing.exists():
        return []
    
    tasks = []
    for project_dir in doing.iterdir():
        if not project_dir.is_dir():
            continue
        tasks_dir = project_dir / "tasks" / "doing"
        if tasks_dir.exists():
            tasks.extend(tasks_dir.glob("*.md"))
    return tasks


def is_stale(task_path: Path, instance_id: str, max_age_minutes: int) -> bool:
    """Check if a task claim is stale (from different instance or too old)."""
    if not task_path.exists():
        return True
    
    txt = task_path.read_text(encoding="utf-8")
    claim = extract_claim_line(txt)
    
    if not claim:
        return True  # Never claimed
    
    # Check if from our instance
    if instance_id not in claim:
        return True  # Claimed by different instance
    
    # Check age
    m = re.search(r"@ (.+?) -->", claim)
    if m:
        try:
            claimed_at = datetime.fromisoformat(m.group(1))
            age = datetime.now() - claimed_at
            if age.total_seconds() > max_age_minutes * 60:
                return True
        except ValueError:
            return True
    
    return False


def extract_dependencies(task_body: str) -> list[str]:
    """Extract task dependencies from task body."""
    deps = []
    for line in task_body.split("\n"):
        line = line.strip().lower()
        if line.startswith("depends-on:") or line.startswith("depends:"):
            value = line.split(":", 1)[1].strip()
            # Split by comma or space
            parts = re.split(r"[,;\s]+", value)
            deps.extend([p.strip() for p in parts if p.strip() and p.startswith("task-")])
    return list(set(deps))

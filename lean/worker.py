"""Worker state machine with full metrics.

Workers are the AI execution units - each runs one task at a time.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class WorkerStatus(Enum):
    """Worker availability status."""
    IDLE = "idle"
    BUSY = "busy"
    COOLDOWN = "cooldown"
    DISABLED = "disabled"


@dataclass
class WorkerMetrics:
    """Full metrics for a worker."""
    jobs_completed: int = 0
    jobs_failed: int = 0
    total_tokens_used: int = 0
    total_duration_seconds: float = 0.0
    last_used_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "jobs_completed": self.jobs_completed,
            "jobs_failed": self.jobs_failed,
            "total_tokens_used": self.total_tokens_used,
            "total_duration_seconds": self.total_duration_seconds,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_failure_at": self.last_failure_at.isoformat() if self.last_failure_at else None,
        }


@dataclass
class Worker:
    """
    Represents an AI worker that executes tasks.
    
    Workers have full state tracking: status, cooldown, metrics.
    """
    id: str
    name: str
    model: str
    base_url: str
    api_key: str
    task_types: list[str] = field(default_factory=lambda: ["general"])
    enabled: bool = True
    status: WorkerStatus = WorkerStatus.IDLE
    cooldown_until: datetime | None = None
    current_job_id: str | None = None
    metrics: WorkerMetrics = field(default_factory=WorkerMetrics)
    
    # From last response
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    def is_available(self, task_type: str) -> bool:
        """Check if worker can handle this task type and is available."""
        if not self.enabled or not self.api_key:
            return False
        if self.status == WorkerStatus.DISABLED:
            return False
        if self.status == WorkerStatus.BUSY:
            return False
        if self.status == WorkerStatus.COOLDOWN:
            if self.cooldown_until and datetime.now() < self.cooldown_until:
                return False
            self.status = WorkerStatus.IDLE
            self.cooldown_until = None
        return task_type in self.task_types or "general" in self.task_types

    def set_cooldown(self, seconds: int) -> None:
        """Put worker in cooldown."""
        self.status = WorkerStatus.COOLDOWN
        self.cooldown_until = datetime.now() + timedelta(seconds=seconds)

    def start_job(self, job_id: str) -> None:
        """Mark worker as starting a job."""
        self.status = WorkerStatus.BUSY
        self.current_job_id = job_id
        self.metrics.last_used_at = datetime.now()

    def finish_job(self, success: bool, tokens_used: int = 0, duration: float = 0.0) -> None:
        """Mark worker as finished with job."""
        self.status = WorkerStatus.IDLE
        if success:
            self.metrics.jobs_completed += 1
            self.metrics.last_success_at = datetime.now()
        else:
            self.metrics.jobs_failed += 1
            self.metrics.last_failure_at = datetime.now()
        self.metrics.total_tokens_used += tokens_used
        self.metrics.total_duration_seconds += duration
        self.current_job_id = None

    def to_config(self) -> dict[str, Any]:
        """Convert to provider config dict."""
        return {
            "name": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "task_types": self.task_types,
            "enabled": self.enabled,
        }

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "Worker":
        """Create worker from provider config."""
        return cls(
            id=config["name"],
            name=config["name"],
            model=config["model"],
            base_url=config["base_url"],
            api_key=config.get("api_key", ""),
            task_types=config.get("task_types", ["general"]),
            enabled=config.get("enabled", True),
        )


class WorkerPool:
    """Manages multiple workers with failover."""
    
    def __init__(self, providers: list[dict[str, Any]]):
        self.workers: list[Worker] = [Worker.from_config(p) for p in providers]
        if not self.workers:
            self.workers = [Worker(id="default", name="default", model="auto", base_url="", api_key="")]

    def get_available(self, task_type: str) -> list[Worker]:
        """Get all available workers for a task type."""
        return [w for w in self.workers if w.is_available(task_type)]

    def get_idle(self) -> Worker | None:
        """Get any idle worker."""
        for w in self.workers:
            if w.is_available("general"):
                return w
        return None

    def call(
        self,
        task_type: str,
        messages: list[dict],
        max_tokens: int = 2000,
    ) -> tuple[Worker, str]:
        """
        Call an available worker.
        
        Returns (worker, response_text).
        Raises RuntimeError if no worker available.
        """
        import requests
        
        candidates = self.get_available(task_type)
        if not candidates:
            candidates = self.get_available("general")
        
        if not candidates:
            raise RuntimeError("No available workers")
        
        last_error = None
        for worker in candidates:
            try:
                headers = {
                    "Authorization": f"Bearer {worker.api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": worker.model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                }
                
                resp = requests.post(
                    f"{worker.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=90,
                )
                
                if resp.status_code == 429:
                    retry_after = 60
                    if "Retry-After" in resp.headers:
                        retry_after = int(resp.headers["Retry-After"])
                    worker.set_cooldown(retry_after)
                    last_error = f"Rate limited ({retry_after}s)"
                    continue
                
                if resp.status_code in (401, 402, 403):
                    worker.enabled = False
                    worker.status = WorkerStatus.DISABLED
                    last_error = f"Auth error ({resp.status_code})"
                    continue
                
                resp.raise_for_status()
                data = resp.json()
                
                worker.finish_reason = data.get("choices", [{}])[0].get("finish_reason")
                worker.usage = data.get("usage", {})
                
                return worker, data["choices"][0]["message"]["content"]
            
            except Exception as e:
                last_error = str(e)
                continue
        
        raise RuntimeError(f"All workers failed: {last_error}")

"""Worker state machine with full metrics and circuit breaker.

Workers are the AI execution units - each runs one task at a time.
Circuit breaker stops hitting dead providers after N consecutive failures.
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


# Default circuit breaker settings
DEFAULT_FAILURE_THRESHOLD = 5  # Disable after N consecutive failures
DEFAULT_RECOVERY_TIMEOUT_MINUTES = 15  # Auto-re-enable after this time


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
    # Circuit breaker state
    consecutive_failures: int = 0
    total_cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "jobs_completed": self.jobs_completed,
            "jobs_failed": self.jobs_failed,
            "total_tokens_used": self.total_tokens_used,
            "total_duration_seconds": self.total_duration_seconds,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_failure_at": self.last_failure_at.isoformat() if self.last_failure_at else None,
            "consecutive_failures": self.consecutive_failures,
            "total_cost_usd": self.total_cost_usd,
        }


@dataclass
class Worker:
    """
    Represents an AI worker that executes tasks.
    
    Workers have full state tracking: status, cooldown, metrics.
    Circuit breaker tracks consecutive failures and disables on threshold.
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
    
    # Circuit breaker settings (can be configured per worker)
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    recovery_timeout_minutes: int = DEFAULT_RECOVERY_TIMEOUT_MINUTES
    
    # From last response
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    
    # Per-model pricing (optional, overrides defaults)
    pricing: dict[str, float] | None = None

    def is_available(self, task_type: str) -> bool:
        """Check if worker can handle this task type and is available."""
        if not self.enabled or not self.api_key:
            return False
        
        # Check circuit breaker - auto-recover after timeout
        if self.status == WorkerStatus.DISABLED:
            if self._should_attempt_recovery():
                self._recover_from_circuit_breaker()
            else:
                return False
        
        if self.status == WorkerStatus.BUSY:
            return False
        
        if self.status == WorkerStatus.COOLDOWN:
            if self.cooldown_until and datetime.now() < self.cooldown_until:
                return False
            self.status = WorkerStatus.IDLE
            self.cooldown_until = None
        
        return task_type in self.task_types or "general" in self.task_types

    def _should_attempt_recovery(self) -> bool:
        """Check if enough time has passed to attempt circuit breaker recovery."""
        if not self.metrics.last_failure_at:
            return True
        elapsed = datetime.now() - self.metrics.last_failure_at
        return elapsed.total_seconds() >= self.recovery_timeout_minutes * 60

    def _recover_from_circuit_breaker(self) -> None:
        """Attempt to recover from circuit breaker by re-enabling."""
        self.status = WorkerStatus.IDLE
        self.metrics.consecutive_failures = 0
        self.enabled = True

    def set_cooldown(self, seconds: int) -> None:
        """Put worker in cooldown (for rate limits)."""
        self.status = WorkerStatus.COOLDOWN
        self.cooldown_until = datetime.now() + timedelta(seconds=seconds)

    def record_failure(self) -> None:
        """
        Record a failure for circuit breaker.
        Disables worker after reaching failure_threshold consecutive failures.
        """
        self.metrics.consecutive_failures += 1
        self.metrics.last_failure_at = datetime.now()
        
        if self.metrics.consecutive_failures >= self.failure_threshold:
            self.status = WorkerStatus.DISABLED
            self.enabled = False

    def record_success(self) -> None:
        """Record a success - resets circuit breaker failure count."""
        self.metrics.consecutive_failures = 0

    def start_job(self, job_id: str) -> None:
        """Mark worker as starting a job."""
        self.status = WorkerStatus.BUSY
        self.current_job_id = job_id
        self.metrics.last_used_at = datetime.now()

    def finish_job(
        self,
        success: bool,
        tokens_used: int = 0,
        duration: float = 0.0,
        cost_usd: float = 0.0,
    ) -> None:
        """Mark worker as finished with job."""
        self.status = WorkerStatus.IDLE
        
        if success:
            self.metrics.jobs_completed += 1
            self.metrics.last_success_at = datetime.now()
            self.record_success()
        else:
            self.metrics.jobs_failed += 1
            self.metrics.last_failure_at = datetime.now()
            self.record_failure()
        
        self.metrics.total_tokens_used += tokens_used
        self.metrics.total_duration_seconds += duration
        self.metrics.total_cost_usd += cost_usd
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
            failure_threshold=config.get("failure_threshold", DEFAULT_FAILURE_THRESHOLD),
            recovery_timeout_minutes=config.get("recovery_timeout_minutes", DEFAULT_RECOVERY_TIMEOUT_MINUTES),
            pricing=config.get("pricing", None),
        )


class WorkerPool:
    """Manages multiple workers with failover and circuit breaker."""
    
    # Rough token pricing per 1M tokens (can be overridden per model)
    # These are defaults - actual costs should be configured per provider
    DEFAULT_PRICING = {
        "input_per_1m": 0.50,   # $0.50 per 1M input tokens
        "output_per_1m": 1.50,  # $1.50 per 1M output tokens
    }
    
    def __init__(self, providers: list[dict[str, Any]]):
        self.workers: list[Worker] = [Worker.from_config(p) for p in providers]
        if not self.workers:
            self.workers = [Worker(id="default", name="default", model="auto", base_url="", api_key="")]
    
    def get_available(self, task_type: str) -> list[Worker]:
        """Get all available workers for a task type."""
        return [w for w in self.workers if w.is_available(task_type)]

    def get_idle(self, task_type: str = "general") -> Worker | None:
        """Get any idle worker that can handle the task type."""
        for w in self.workers:
            if w.is_available(task_type):
                return w
        return None

    def get_pool_status(self) -> dict[str, Any]:
        """Get overall pool status including circuit breaker state."""
        return {
            "total_workers": len(self.workers),
            "available": len([w for w in self.workers if w.status == WorkerStatus.IDLE]),
            "busy": len([w for w in self.workers if w.status == WorkerStatus.BUSY]),
            "cooldown": len([w for w in self.workers if w.status == WorkerStatus.COOLDOWN]),
            "disabled": len([w for w in self.workers if w.status == WorkerStatus.DISABLED]),
        }

    @staticmethod
    def estimate_cost(usage: dict[str, Any], pricing: dict[str, float] | None = None) -> float:
        """Estimate cost in USD based on token usage."""
        if pricing is None:
            pricing = WorkerPool.DEFAULT_PRICING
        
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        
        input_cost = (input_tokens / 1_000_000) * pricing.get("input_per_1m", 0.50)
        output_cost = (output_tokens / 1_000_000) * pricing.get("output_per_1m", 1.50)
        
        return input_cost + output_cost

    def call(
        self,
        task_type: str,
        messages: list[dict],
        max_tokens: int = 2000,
        timeout: int = 90,
    ) -> tuple[Worker, str, float]:
        """
        Call an available worker.
        
        Returns (worker, response_text, cost_usd).
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
                    timeout=timeout,
                )
                
                if resp.status_code == 429:
                    retry_after = 60
                    if "Retry-After" in resp.headers:
                        retry_after = int(resp.headers["Retry-After"])
                    worker.set_cooldown(retry_after)
                    worker.record_failure()
                    last_error = f"Rate limited ({retry_after}s)"
                    continue
                
                if resp.status_code in (401, 402, 403):
                    worker.enabled = False
                    worker.status = WorkerStatus.DISABLED
                    worker.record_failure()
                    last_error = f"Auth error ({resp.status_code})"
                    continue
                
                resp.raise_for_status()
                data = resp.json()
                
                worker.finish_reason = data.get("choices", [{}])[0].get("finish_reason")
                worker.usage = data.get("usage", {})
                
                # Calculate and return cost using worker-specific pricing
                cost = self.estimate_cost(worker.usage, worker.pricing)
                
                return worker, data["choices"][0]["message"]["content"], cost
            
            except requests.Timeout:
                worker.record_failure()
                last_error = "Request timeout"
                continue
            except Exception as e:
                worker.record_failure()
                last_error = str(e)
                continue
        
        raise RuntimeError(f"All workers failed: {last_error}")
    
    def call_with(
        self,
        worker: "Worker",
        messages: list[dict],
        max_tokens: int = 2000,
        timeout: int = 90,
    ) -> tuple["Worker", str, float]:
        """
        Call a specific worker directly (no selection logic).
        
        Used when a worker has already been reserved for a task.
        The caller is responsible for ensuring the worker is available.
        
        Returns (worker, response_text, cost_usd).
        Raises RuntimeError on failure.
        """
        import requests
        
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
                timeout=timeout,
            )
            
            if resp.status_code == 429:
                retry_after = 60
                if "Retry-After" in resp.headers:
                    retry_after = int(resp.headers["Retry-After"])
                worker.set_cooldown(retry_after)
                worker.record_failure()
                raise RuntimeError(f"Rate limited ({retry_after}s)")
            
            if resp.status_code in (401, 402, 403):
                worker.enabled = False
                worker.status = WorkerStatus.DISABLED
                worker.record_failure()
                raise RuntimeError(f"Auth error ({resp.status_code})")
            
            resp.raise_for_status()
            data = resp.json()
            
            worker.finish_reason = data.get("choices", [{}])[0].get("finish_reason")
            worker.usage = data.get("usage", {})
            
            # Calculate and return cost using worker-specific pricing
            cost = self.estimate_cost(worker.usage, worker.pricing)
            
            return worker, data["choices"][0]["message"]["content"], cost
        
        except requests.Timeout:
            worker.record_failure()
            raise RuntimeError("Request timeout")
        except RuntimeError:
            raise
        except Exception as e:
            worker.record_failure()
            raise RuntimeError(str(e))

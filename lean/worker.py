"""
Worker module for Vault Orchestrator.

Handles provider pool, rate limiting, and task type routing.
Per §10.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config, WorkerConfig


@dataclass
class Worker:
    """
    A worker/provider with runtime state.
    
    Includes cooldown tracking for rate limiting.
    """
    config: "WorkerConfig"
    last_used: float = 0
    cooldown_until: float = 0
    long_cooldown_until: float = 0  # For auth errors
    
    @property
    def name(self) -> str:
        return self.config.name
    
    @property
    def model(self) -> str:
        return self.config.model
    
    @property
    def base_url(self) -> str | None:
        return self.config.base_url
    
    @property
    def api_key(self) -> str | None:
        return self.config.api_key
    
    @property
    def task_types(self) -> list[str]:
        return self.config.task_types
    
    @property
    def is_available(self) -> bool:
        """Check if worker is available (not in cooldown)."""
        now = time.time()
        return now >= self.cooldown_until and now >= self.long_cooldown_until
    
    def set_cooldown(self, retry_after: int | None = None) -> None:
        """
        Set short-term cooldown (rate limit).
        
        Respects Retry-After header if provided.
        """
        if retry_after:
            self.cooldown_until = time.time() + retry_after
        else:
            self.cooldown_until = time.time() + 60  # Default 1 minute
    
    def set_long_cooldown(self) -> None:
        """
        Set long-term cooldown (auth error).
        
        For 401/402/403 errors - a dead/invalid key shouldn't be
        retried every cycle the way a rate limit should.
        """
        self.long_cooldown_until = time.time() + 3600  # 1 hour
    
    def clear_cooldowns(self) -> None:
        """Clear all cooldowns."""
        self.cooldown_until = 0
        self.long_cooldown_until = 0


class WorkerPool:
    """
    Pool of workers with rate limiting and routing.
    
    Per §10.
    """
    
    def __init__(self, config: "Config"):
        self._config = config
        self._workers: dict[str, Worker] = {}
        
        # Initialize workers from config
        for worker_config in config.workers:
            self._workers[worker_config.name] = Worker(config=worker_config)
    
    def get_worker(self, name: str) -> Worker | None:
        """Get a worker by name."""
        return self._workers.get(name)
    
    def get_available_worker(self, task_type: str) -> Worker | None:
        """
        Get an available worker for a task type.
        
        Returns None if no worker is available.
        """
        available = []
        
        for worker in self._workers.values():
            if not worker.is_available:
                continue
            
            # Check task type compatibility
            if worker.task_types and task_type not in worker.task_types:
                continue
            
            available.append(worker)
        
        if not available:
            return None
        
        # Return least recently used
        return min(available, key=lambda w: w.last_used)
    
    def get_all_workers(self) -> list[Worker]:
        """Get all workers."""
        return list(self._workers.values())
    
    def get_available_workers(self) -> list[Worker]:
        """Get all available workers."""
        return [w for w in self._workers.values() if w.is_available]
    
    def mark_used(self, worker_name: str) -> None:
        """Mark a worker as used."""
        worker = self._workers.get(worker_name)
        if worker:
            worker.last_used = time.time()
    
    def mark_rate_limited(self, worker_name: str, retry_after: int | None = None) -> None:
        """Mark a worker as rate limited."""
        worker = self._workers.get(worker_name)
        if worker:
            worker.set_cooldown(retry_after)
    
    def mark_auth_failed(self, worker_name: str) -> None:
        """Mark a worker as having an auth failure."""
        worker = self._workers.get(worker_name)
        if worker:
            worker.set_long_cooldown()
    
    def update_from_config(self, config: "Config") -> None:
        """
        Update pool from config, preserving cooldown state.
        
        Per §10: Settings changes update the pool in place, not by
        constructing a fresh pool object - a fresh object would
        reset in-memory cooldown state.
        """
        # Remove workers not in new config
        new_names = {w.name for w in config.workers}
        for name in list(self._workers.keys()):
            if name not in new_names:
                del self._workers[name]
        
        # Add or update workers
        for worker_config in config.workers:
            if worker_config.name in self._workers:
                # Update config, preserve cooldown state
                self._workers[worker_config.name].config = worker_config
            else:
                # Add new worker
                self._workers[worker_config.name] = Worker(config=worker_config)
        
        self._config = config
    
    def status(self) -> dict[str, Any]:
        """Get pool status for diagnostics."""
        now = time.time()
        return {
            "total": len(self._workers),
            "available": len(self.get_available_workers()),
            "workers": [
                {
                    "name": w.name,
                    "model": w.model,
                    "available": w.is_available,
                    "in_cooldown": now < w.cooldown_until,
                    "in_long_cooldown": now < w.long_cooldown_until,
                    "task_types": w.task_types,
                }
                for w in self._workers.values()
            ],
        }

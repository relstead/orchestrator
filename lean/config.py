"""
Configuration module for Vault Orchestrator.

Loads and validates configuration from config.json in the vault root.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WorkerConfig:
    """Configuration for a single worker/provider."""
    name: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    task_types: list[str] = field(default_factory=list)


@dataclass
class BudgetConfig:
    """Budget and limit configuration."""
    max_turns: int = 6
    max_plan_turns: int = 10
    max_tasks_per_plan: int = 12
    max_new_projects_per_plan: int = 3
    max_plan_depth: int = 2
    max_body_chars: int = 2000
    coding_execute_timeout: int = 30
    plan_execute_timeout: int = 10
    max_backups_per_project: int = 5
    context_top_n: int = 5
    context_max_chars: int = 6000


@dataclass
class MetricsConfig:
    """Metrics configuration."""
    compact_at_bytes: int = 1048576  # 1MB


@dataclass
class Config:
    """Main configuration for the orchestrator."""
    workers: list[WorkerConfig] = field(default_factory=list)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    use_planning: bool = True
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        """Create Config from a dictionary."""
        workers = [
            WorkerConfig(**w) for w in data.get("workers", [])
        ]
        
        budgets_data = data.get("budgets", {})
        budgets = BudgetConfig(**budgets_data) if budgets_data else BudgetConfig()
        
        metrics_data = data.get("metrics", {})
        metrics = MetricsConfig(**metrics_data) if metrics_data else MetricsConfig()
        
        return cls(
            workers=workers,
            budgets=budgets,
            metrics=metrics,
            use_planning=data.get("use_planning", True),
        )
    
    @classmethod
    def from_file(cls, path: Path) -> "Config":
        """Load configuration from a JSON file."""
        if not path.exists():
            return cls()
        
        with open(path) as f:
            data = json.load(f)
        
        return cls.from_dict(data)
    
    def to_dict(self) -> dict[str, Any]:
        """Convert Config to a dictionary."""
        return {
            "workers": [
                {
                    "name": w.name,
                    "model": w.model,
                    "base_url": w.base_url,
                    "api_key": w.api_key,
                    "task_types": w.task_types,
                }
                for w in self.workers
            ],
            "budgets": {
                "max_turns": self.budgets.max_turns,
                "max_plan_turns": self.budgets.max_plan_turns,
                "max_tasks_per_plan": self.budgets.max_tasks_per_plan,
                "max_new_projects_per_plan": self.budgets.max_new_projects_per_plan,
                "max_plan_depth": self.budgets.max_plan_depth,
                "max_body_chars": self.budgets.max_body_chars,
                "coding_execute_timeout": self.budgets.coding_execute_timeout,
                "plan_execute_timeout": self.budgets.plan_execute_timeout,
                "max_backups_per_project": self.budgets.max_backups_per_project,
                "context_top_n": self.budgets.context_top_n,
                "context_max_chars": self.budgets.context_max_chars,
            },
            "metrics": {
                "compact_at_bytes": self.metrics.compact_at_bytes,
            },
            "use_planning": self.use_planning,
        }
    
    def save(self, path: Path) -> None:
        """Save configuration to a JSON file."""
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
    
    def get_worker(self, name: str) -> WorkerConfig | None:
        """Get a worker by name."""
        for worker in self.workers:
            if worker.name == name:
                return worker
        return None
    
    def get_workers_for_task_type(self, task_type: str) -> list[WorkerConfig]:
        """Get all workers that can handle a task type."""
        return [
            w for w in self.workers
            if not w.task_types or task_type in w.task_types
        ]

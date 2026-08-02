"""
Planner module for Vault Orchestrator.

Handles propose_plan validation only - never calls a provider directly.
Per §13 and §8.4.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config
    from .tasks import TaskStore, TaskState


@dataclass
class PlanValidationResult:
    """Result of plan validation."""
    valid: bool
    error: str | None = None
    validated_tasks: list[dict[str, Any]] | None = None


class PlannerValidator:
    """
    Validates proposed plans.
    
    Per §13: planner.py owns validation only; it never calls a provider
    directly or manages transcript state - that stays in the orchestrator.
    """
    
    def __init__(self, config: "Config"):
        self._config = config
    
    def validate_batch(
        self,
        tasks: list[dict[str, Any]],
        existing_task_ids: set[str],
        all_vault_paths: set[str],
    ) -> PlanValidationResult:
        """
        Validate a batch of proposed tasks.
        
        Checks per §15 tests 19-22:
        - No cyclic dependencies
        - depth <= max_plan_depth
        - body length <= max_body_chars
        - context_hint paths are inside vault
        - depends-on references exist
        
        Returns (valid, error_message, validated_tasks).
        """
        budgets = self._config.budgets
        
        # Check batch size
        if len(tasks) > budgets.max_tasks_per_plan:
            return PlanValidationResult(
                valid=False,
                error=f"Batch has {len(tasks)} tasks, max is {budgets.max_tasks_per_plan}",
            )
        
        # Build dependency graph for cycle detection
        deps: dict[str, set[str]] = {}
        task_ids: set[str] = set()
        
        validated = []
        
        for task_data in tasks:
            task_id = task_data.get("id", "")
            if not task_id:
                return PlanValidationResult(
                    valid=False,
                    error="Task missing id",
                )
            
            if task_id in task_ids:
                return PlanValidationResult(
                    valid=False,
                    error=f"Duplicate task id: {task_id}",
                )
            
            task_ids.add(task_id)
            deps[task_id] = set()
            
            # Check depth
            depth = task_data.get("depth", 0)
            if depth > budgets.max_plan_depth:
                return PlanValidationResult(
                    valid=False,
                    error=f"Task {task_id} has depth {depth}, max is {budgets.max_plan_depth}",
                )
            
            # Check body length
            body = task_data.get("body", "")
            if len(body) > budgets.max_body_chars:
                return PlanValidationResult(
                    valid=False,
                    error=f"Task {task_id} body is {len(body)} chars, max is {budgets.max_body_chars}",
                )
            
            # Check context_hint
            context_hint = task_data.get("context_hint", "")
            if context_hint:
                if context_hint not in all_vault_paths:
                    return PlanValidationResult(
                        valid=False,
                        error=f"Task {task_id} has invalid context_hint: {context_hint}",
                    )
            
            # Parse depends-on
            depends_on_str = task_data.get("depends-on", "")
            dep_ids = [d.strip() for d in depends_on_str.split(",") if d.strip()]
            
            for dep_id in dep_ids:
                # Check depends-on reference exists (in batch or existing)
                if dep_id not in task_ids and dep_id not in existing_task_ids:
                    return PlanValidationResult(
                        valid=False,
                        error=f"Task {task_id} depends on unknown task: {dep_id}",
                    )
                deps[task_id].add(dep_id)
            
            validated.append({
                **task_data,
                "depends_on": dep_ids,
            })
        
        # Check for cycles
        if self._has_cycle(deps):
            return PlanValidationResult(
                valid=False,
                error="Batch has cyclic dependencies",
            )
        
        return PlanValidationResult(
            valid=True,
            validated_tasks=validated,
        )
    
    def _has_cycle(self, deps: dict[str, set[str]]) -> bool:
        """Check if dependency graph has a cycle using DFS."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {node: WHITE for node in deps}
        
        def dfs(node: str) -> bool:
            color[node] = GRAY
            for neighbor in deps.get(node, set()):
                if neighbor not in color:
                    continue
                if color[neighbor] == GRAY:
                    return True
                if color[neighbor] == WHITE:
                    if dfs(neighbor):
                        return True
            color[node] = BLACK
            return False
        
        for node in deps:
            if color[node] == WHITE:
                if dfs(node):
                    return True
        
        return False
    
    def parse_plan_response(self, response: str) -> list[dict[str, Any]]:
        """
        Parse a plan response into task dictionaries.
        
        Expects JSON array of task objects.
        """
        from .agent import extract_json
        
        json_str = extract_json(response)
        if not json_str:
            return []
        
        try:
            data = json.loads(json_str)
            if isinstance(data, list):
                return data
            elif isinstance(data, dict) and "tasks" in data:
                return data["tasks"]
        except json.JSONDecodeError:
            pass
        
        return []
    
    def should_create_project(
        self,
        project_name: str,
        current_project_count: int,
        max_projects: int | None = None,
    ) -> bool:
        """
        Check if a new project should be created.
        
        Limits new projects per plan per budgets.max_new_projects_per_plan.
        """
        if max_projects is None:
            max_projects = self._config.budgets.max_new_projects_per_plan
        
        return current_project_count < max_projects

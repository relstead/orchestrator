"""
Dependency module for Vault Orchestrator.

Handles task dependency graph, cycle detection, and blocked/pending resolution.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from .tasks import Task, TaskStore, TaskState
    from .vault import Vault


@dataclass
class DependencyGraph:
    """
    Task dependency graph.
    
    Builds a DAG from task dependencies and detects cycles.
    Per §6 and §7.
    """
    
    # Adjacency: task_id -> set of task_ids it depends on
    edges: dict[str, set[str]] = field(default_factory=dict)
    
    # Reverse adjacency: task_id -> set of tasks that depend on it
    dependents: dict[str, set[str]] = field(default_factory=dict)
    
    # Task metadata cache
    tasks: dict[str, Task] = field(default_factory=dict)
    
    def add_task(self, task: Task) -> None:
        """Add a task to the graph."""
        self.tasks[task.id] = task
        if task.id not in self.edges:
            self.edges[task.id] = set()
        if task.id not in self.dependents:
            self.dependents[task.id] = set()
        
        # Add dependency edges
        for dep_id in task.meta.depends_on:
            self.add_dependency(task.id, dep_id)
    
    def add_dependency(self, task_id: str, depends_on: str) -> None:
        """Add a dependency edge."""
        if task_id not in self.edges:
            self.edges[task_id] = set()
        self.edges[task_id].add(depends_on)
        
        if depends_on not in self.dependents:
            self.dependents[depends_on] = set()
        self.dependents[depends_on].add(task_id)
    
    def has_cycle(self) -> bool:
        """Check if the graph has a cycle."""
        visited = set()
        rec_stack = set()
        
        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            
            for neighbor in self.edges.get(node, set()):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            
            rec_stack.remove(node)
            return False
        
        for node in self.edges:
            if node not in visited:
                if dfs(node):
                    return True
        
        return False
    
    def get_ready_tasks(self) -> list[str]:
        """
        Get tasks that are ready to run.
        
        A task is ready if:
        1. It's in pending state
        2. All its dependencies are satisfied (in done state)
        """
        ready = []
        
        for task_id, task in self.tasks.items():
            if task.state.value != "pending":
                continue
            
            deps = self.edges.get(task_id, set())
            
            # Check all dependencies are done
            all_done = True
            for dep_id in deps:
                dep_task = self.tasks.get(dep_id)
                if dep_task is None:
                    # Dependency doesn't exist - task is blocked
                    all_done = False
                    break
                if dep_task.state.value != "done":
                    all_done = False
                    break
            
            if all_done:
                ready.append(task_id)
        
        return ready
    
    def get_blocked_tasks(self) -> list[str]:
        """
        Get tasks that are blocked.
        
        A task is blocked if:
        1. It's in pending state
        2. It has an unresolvable dependency
        """
        blocked = []
        
        for task_id, task in self.tasks.items():
            if task.state.value != "pending":
                continue
            
            deps = self.edges.get(task_id, set())
            
            for dep_id in deps:
                if dep_id not in self.tasks:
                    # Dependency doesn't exist - blocked
                    blocked.append(task_id)
                    break
        
        return blocked
    
    def topological_sort(self) -> list[str]:
        """
        Return tasks in topological order.
        
        Raises ValueError if the graph has cycles.
        """
        if self.has_cycle():
            raise ValueError("Cannot sort: graph has cycles")
        
        in_degree = defaultdict(int)
        for task_id in self.tasks:
            in_degree[task_id]  # Ensure exists
        
        for task_id, deps in self.edges.items():
            for dep_id in deps:
                if dep_id in in_degree:
                    in_degree[task_id] += 1
        
        # Kahn's algorithm
        queue = [t for t, d in in_degree.items() if d == 0]
        result = []
        
        while queue:
            node = queue.pop(0)
            result.append(node)
            
            for dependent in self.dependents.get(node, set()):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
        
        return result


def build_graph(task_store: "TaskStore", projects: list[str]) -> DependencyGraph:
    """
    Build a dependency graph from tasks in the given projects.
    
    Returns a tuple of (graph, blocked_task_ids).
    """
    graph = DependencyGraph()
    blocked = []
    
    # Collect all tasks
    for project in projects:
        for state in ["pending", "doing", "done", "blocked", "waiting", "failed"]:
            for task in task_store.get_tasks_in_state(project, state):
                graph.add_task(task)
    
    # Check for blocked tasks
    blocked = graph.get_blocked_tasks()
    
    return graph, blocked


def resolve_blocked_tasks(
    task_store: "TaskStore",
    graph: DependencyGraph,
) -> list[tuple[str, str]]:
    """
    Move blocked tasks to blocked/ directory.
    
    Returns list of (task_id, project) for moved tasks.
    """
    from .tasks import TaskState
    
    moved = []
    
    for task_id in graph.get_blocked_tasks():
        task = graph.tasks.get(task_id)
        if not task:
            continue
        
        if task.state == TaskState.PENDING:
            task_store.move_task(task, TaskState.BLOCKED)
            moved.append((task_id, task.project))
    
    return moved


def validate_batch(
    tasks: list[dict],
    max_depth: int,
    max_body_chars: int,
    max_tasks: int,
    all_task_ids: set[str],
    vault_paths: set[str],
) -> tuple[bool, str]:
    """
    Validate a batch of proposed tasks.
    
    Returns (valid, error_message).
    
    Checks:
    - No cycles in dependencies
    - depth doesn't exceed max_depth
    - body doesn't exceed max_body_chars
    - context_hint paths are inside the vault
    - all depends-on references exist
    """
    if len(tasks) > max_tasks:
        return False, f"Batch has {len(tasks)} tasks, max is {max_tasks}"
    
    # Build mini graph for cycle detection
    mini_graph = DependencyGraph()
    
    for task_data in tasks:
        task_id = task_data.get("id", "")
        if not task_id:
            return False, "Task missing id"
        
        depends_on = task_data.get("depends-on", "")
        deps = [d.strip() for d in depends_on.split(",") if d.strip()]
        
        # Check depth
        depth = task_data.get("depth", 0)
        if depth > max_depth:
            return False, f"Task {task_id} has depth {depth}, max is {max_depth}"
        
        # Check body length
        body = task_data.get("body", "")
        if len(body) > max_body_chars:
            return False, f"Task {task_id} body is {len(body)} chars, max is {max_body_chars}"
        
        # Check context_hint
        context_hint = task_data.get("context_hint", "")
        if context_hint:
            # Check if path is inside vault
            # For now, just check it exists in our known paths
            if context_hint not in vault_paths:
                return False, f"Task {task_id} has invalid context_hint: {context_hint}"
        
        # Check depends-on references
        for dep_id in deps:
            if dep_id not in all_task_ids:
                return False, f"Task {task_id} depends on unknown task: {dep_id}"
        
        # Add to mini graph
        mini_graph.tasks[task_id] = True  # Just mark existence
        for dep_id in deps:
            mini_graph.add_dependency(task_id, dep_id)
    
    # Check for cycles
    if mini_graph.has_cycle():
        return False, "Batch has cyclic dependencies"
    
    return True, ""

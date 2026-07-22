"""Dependency graph (DAG) for task ordering.

Builds a DAG from task dependencies, resolves ready tasks,
detects cycles.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .tasks import parse_task, extract_dependencies


@dataclass
class TaskNode:
    """A task in the dependency graph."""
    id: str
    path: Path
    title: str
    task_type: str
    attempts: int
    body: str
    depends_on: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    status: str = "pending"  # pending, running, done, failed, blocked


class DependencyGraph:
    """Manages task dependencies as a DAG."""
    
    def __init__(self):
        self.nodes: dict[str, TaskNode] = {}
        self._has_cycle: bool | None = None

    def add_task(self, path: Path) -> TaskNode | None:
        """Add a task to the graph."""
        try:
            task = parse_task(path)
            
            # Generate ID from filename
            task_id = path.stem
            if not task_id.startswith("task-"):
                task_id = f"task-{task_id}"
            
            # Extract dependencies
            deps = extract_dependencies(task.body)
            
            node = TaskNode(
                id=task_id,
                path=path,
                title=task.body.split("\n")[0][:50] if task.body else "Untitled",
                task_type=task.task_type,
                attempts=task.attempts,
                body=task.body,
                depends_on=deps,
            )
            
            self.nodes[task_id] = node
            
            # Update dependents
            for dep_id in deps:
                if dep_id in self.nodes:
                    self.nodes[dep_id].dependents.append(task_id)
            
            return node
        except Exception:
            return None

    def update_status(self, task_id: str, status: str) -> None:
        """Update a task's status."""
        if task_id in self.nodes:
            self.nodes[task_id].status = status

    def get_ready(self) -> list[TaskNode]:
        """Get tasks with all dependencies satisfied."""
        ready = []
        for node in self.nodes.values():
            if node.status != "pending":
                continue
            
            deps_satisfied = all(
                self.nodes.get(dep_id) and self.nodes[dep_id].status == "done"
                for dep_id in node.depends_on
                if dep_id in self.nodes
            )
            
            if deps_satisfied:
                ready.append(node)
        
        return ready

    def get_blocked(self) -> list[tuple[TaskNode, list[str]]]:
        """Get tasks blocked by dependencies."""
        blocked = []
        for node in self.nodes.values():
            if node.status != "pending":
                continue
            
            unfulfilled = []
            for dep_id in node.depends_on:
                dep = self.nodes.get(dep_id)
                if dep is None or dep.status != "done":
                    unfulfilled.append(dep_id)
            
            if unfulfilled:
                blocked.append((node, unfulfilled))
        
        return blocked

    def detect_cycle(self) -> list[str] | None:
        """Detect if there's a cycle in the graph."""
        visited = set()
        rec_stack = set()
        path = []

        def dfs(node_id: str) -> list[str] | None:
            if node_id not in self.nodes:
                return None
            visited.add(node_id)
            rec_stack.add(node_id)
            path.append(node_id)

            for dep_id in self.nodes[node_id].depends_on:
                if dep_id not in self.nodes:
                    continue
                if dep_id not in visited:
                    result = dfs(dep_id)
                    if result:
                        return result
                elif dep_id in rec_stack:
                    cycle_start = path.index(dep_id)
                    return path[cycle_start:] + [dep_id]

            path.pop()
            rec_stack.remove(node_id)
            return None

        for node_id in self.nodes:
            if node_id not in visited:
                cycle = dfs(node_id)
                if cycle:
                    return cycle

        return None

    def topological_sort(self) -> list[TaskNode] | None:
        """Return tasks in dependency order. None if cycle."""
        in_degree = {nid: len(n.depends_on) for nid, n in self.nodes.items()}
        
        # Reduce for satisfied deps
        for nid, node in self.nodes.items():
            satisfied = sum(
                1 for dep_id in node.depends_on
                if dep_id in self.nodes and self.nodes[dep_id].status == "done"
            )
            in_degree[nid] -= satisfied

        queue = [nid for nid, deg in in_degree.items() if deg <= 0]
        result = []

        while queue:
            nid = queue.pop(0)
            result.append(self.nodes[nid])

            for dep_nid in self.nodes[nid].dependents:
                if dep_nid in in_degree:
                    in_degree[dep_nid] -= 1
                    if in_degree[dep_nid] <= 0:
                        queue.append(dep_nid)

        if len(result) != len(self.nodes):
            self._has_cycle = True
            return None

        self._has_cycle = False
        return result


def build_graph_from_paths(paths: list[Path]) -> DependencyGraph:
    """Build a dependency graph from task file paths."""
    graph = DependencyGraph()
    for path in paths:
        graph.add_task(path)
    return graph

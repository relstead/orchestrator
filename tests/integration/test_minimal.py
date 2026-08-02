"""
Minimal Integration Test - Canary Test for Bedrock

This test verifies the core workflow works end-to-end:
1. Create a temp vault
2. Create a project
3. Create a task
4. Move task through pending → doing → done
5. Verify task file lands in done/ with correct meta

This is the "canary" test - if it passes, the bedrock is solid enough for internals work.
"""

import tempfile
from pathlib import Path

import pytest


class TestMinimalIntegration:
    """Minimal integration test for bedrock."""

    def test_create_vault_and_project(self):
        """Create a vault and project."""
        from lean.vault import Vault, open_vault
        
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            
            # Create vault
            vault = open_vault(vault_path)
            
            # Verify skeleton created
            assert vault.projects_path.exists()
            assert vault.backups_path.exists()
            assert vault.metrics_path.exists()
            assert vault.skills_path.exists()

    def test_create_and_retrieve_task(self):
        """Create a task and retrieve it."""
        from lean.vault import open_vault
        from lean.tasks import TaskStore, TaskState, TaskType
        from lean.config import Config
        
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            config = Config()
            
            # Create vault
            vault = open_vault(vault_path)
            task_store = TaskStore(vault)
            
            # Create project
            project_path = vault.ensure_project_skeleton("test-project")
            
            # Create task
            task = task_store.create_task(
                project="test-project",
                title="Test Task",
                body="This is a test task body",
                task_type=TaskType.CODING,
            )
            
            assert task is not None
            assert task.id.startswith("task-")
            assert task.title == "Test Task"
            assert task.state == TaskState.PENDING
            
            # Retrieve task
            retrieved = task_store.get_task("test-project", task.id)
            assert retrieved is not None
            assert retrieved.title == task.title

    def test_move_task_through_states(self):
        """Move task through pending → doing → done."""
        from lean.vault import open_vault
        from lean.tasks import TaskStore, TaskState, TaskType
        
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            
            # Create vault
            vault = open_vault(vault_path)
            task_store = TaskStore(vault)
            
            # Create task
            task = task_store.create_task(
                project="test-project",
                title="State Machine Test",
                body="Testing state transitions",
                task_type=TaskType.CODING,
            )
            
            assert task.state == TaskState.PENDING
            
            # Move to doing
            task = task_store.move_task(task, TaskState.DOING)
            assert task.state == TaskState.DOING
            
            # Verify in doing/ directory
            tasks_in_doing = task_store.get_tasks_in_state("test-project", TaskState.DOING)
            assert len(tasks_in_doing) == 1
            assert tasks_in_doing[0].id == task.id
            
            # Move to done
            task = task_store.move_task(task, TaskState.DONE)
            assert task.state == TaskState.DONE
            
            # Verify in done/ directory
            tasks_in_done = task_store.get_tasks_in_state("test-project", TaskState.DONE)
            assert len(tasks_in_done) == 1
            assert tasks_in_done[0].id == task.id

    def test_task_with_dependencies(self):
        """Create tasks with dependencies."""
        from lean.vault import open_vault
        from lean.tasks import TaskStore, TaskState, TaskType
        from lean.dependency import DependencyGraph, build_graph
        
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            
            vault = open_vault(vault_path)
            task_store = TaskStore(vault)
            
            # Create tasks
            task1 = task_store.create_task(
                project="test-project",
                title="Task 1",
                body="First task",
                task_type=TaskType.CODING,
            )
            
            task2 = task_store.create_task(
                project="test-project",
                title="Task 2",
                body="Depends on Task 1",
                task_type=TaskType.CODING,
                depends_on=[task1.id],
            )
            
            # Build dependency graph
            graph, blocked = build_graph(task_store, ["test-project"])
            
            # Both tasks should be in the graph
            assert task1.id in graph.tasks
            assert task2.id in graph.tasks
            
            # Task1 should be ready (no deps that aren't done)
            ready = graph.get_ready_tasks()
            assert task1.id in ready
            
            # Task2 has a valid dependency (task1 exists), so not blocked
            # It's in pending but waiting for task1 to complete
            blocked_ids = graph.get_blocked_tasks()
            assert task2.id not in blocked_ids  # Task1 exists, so not blocked

    def test_snapshot_and_changeset(self):
        """Test snapshot and changeset tracking."""
        from lean.vault import open_vault
        from lean.tasks import TaskStore, TaskType
        from lean.config import Config
        from lean.orchestrator import Orchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            config = Config()
            
            # Create vault and orchestrator
            vault = open_vault(vault_path)
            task_store = TaskStore(vault)
            
            # Create a project with a file
            project_path = vault.ensure_project_skeleton("test-project")
            (project_path / "test.txt").write_text("test content")
            
            # Create orchestrator
            orch = Orchestrator(vault_path, config)
            
            # Take snapshot
            snapshot_path = orch.ensure_snapshot("task-01", 1, project_path)
            assert snapshot_path is not None
            
            # Verify snapshot taken
            assert "snapshot_task-01_1" in snapshot_path
            assert (vault.backups_path / "snapshot_task-01_1").exists()
            
            # Track write in changeset
            orch.track_write("/Projects/test-project/new-file.txt", "created")
            assert "/Projects/test-project/new-file.txt" in orch._state.changeset
            
            # Save changeset
            orch.save_changeset("task-01")
            changeset_path = vault.backups_path / "changeset_task-01.json"
            assert changeset_path.exists()
            
            # Load changeset
            loaded = orch.load_changeset("task-01")
            assert "/Projects/test-project/new-file.txt" in loaded

    def test_output_compression(self):
        """Test output compression."""
        from lean.vault import open_vault
        from lean.config import Config
        from lean.orchestrator import Orchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            config = Config()
            
            vault = open_vault(vault_path)
            orch = Orchestrator(vault_path, config)
            
            # Short output - not compressed
            short = "line1\nline2\nline3"
            result = orch.compress_output(short)
            assert result == short
            
            # Long output - compressed
            long = "\n".join(f"line{i}" for i in range(50))
            result = orch.compress_output(long, max_chars=200)
            assert "... [truncated" in result
            assert len(result) < len(long)

    def test_inbox_parsing(self):
        """Test inbox parsing."""
        from lean.vault import open_vault
        from lean.config import Config
        from lean.orchestrator import Orchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            config = Config()
            
            vault = open_vault(vault_path)
            orch = Orchestrator(vault_path, config)
            
            # Test parsing
            content = """[project1] First Task
This is the body

@project2: Second Task
More body text

---
[project1] Third Task
Body after separator"""
            
            items = orch._parse_inbox_items(content)
            
            assert len(items) == 4
            assert items[0]["project"] == "project1"
            assert items[0]["title"] == "First Task"
            assert items[1]["project"] == "project2"
            assert items[1]["title"] == "Second Task"
            assert items[2]["project"] == "project2"
            assert items[2]["title"] == "Second Task"
            assert items[3]["project"] == "project1"
            assert items[3]["title"] == "Third Task"

    def test_task_type_inference(self):
        """Test task type inference from body."""
        from lean.vault import open_vault
        from lean.config import Config
        from lean.orchestrator import Orchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            config = Config()
            
            vault = open_vault(vault_path)
            orch = Orchestrator(vault_path, config)
            
            # Coding task
            assert orch._infer_task_type("Fix the login bug") == "coding"
            assert orch._infer_task_type("Update the docs") == "coding"
            
            # Plan task
            assert orch._infer_task_type("Plan the architecture") == "plan"
            assert orch._infer_task_type("Research alternatives") == "plan"
            assert orch._infer_task_type("Design the system breakdown") == "plan"

    def test_metrics_writer(self):
        """Test metrics writer."""
        from lean.logger import MetricsWriter, TaskEvent
        
        with tempfile.TemporaryDirectory() as tmpdir:
            metrics = MetricsWriter(Path(tmpdir))
            
            # Create event
            event = TaskEvent.create(
                task_id="task-01",
                task_type="coding",
                turns_used=3,
                max_turns=6,
                outcome="done",
            )
            
            # Append
            metrics.append(event)
            
            # Verify written
            assert metrics.events_path.exists()
            events = metrics.read_events()
            assert len(events) == 1
            assert events[0].task_id == "task-01"

    def test_compaction_cap(self):
        """Test compaction only runs once per cycle."""
        from lean.vault import open_vault
        from lean.config import Config
        from lean.orchestrator import Orchestrator
        
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            config = Config()
            
            vault = open_vault(vault_path)
            orch = Orchestrator(vault_path, config)
            
            # Simulate poll cycle
            orch._state.compaction_ran_this_cycle = False
            orch._state.compaction_backoff_until = 0
            
            # First call - should run
            orch._maybe_compact()
            # (No metrics to compact, so just verify state)
            assert orch._state.compaction_ran_this_cycle == False  # No metrics to compact
            
            # Second call in same cycle - should not run
            orch._state.compaction_ran_this_cycle = True
            orch._maybe_compact()
            # State should remain True (not reset)
            assert orch._state.compaction_ran_this_cycle == True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

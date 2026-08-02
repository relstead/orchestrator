# Internals Contract

This document defines the interface between **bedrock** (already implemented) and **internals** (what comes next).

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Orchestrator (bedrock)                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Task Store   │  │ Indexer      │  │ Worker Pool      │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Sandbox      │  │ Metrics      │  │ Digest           │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   Worker Thread (internals)                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Agent Loop    │  │ Provider     │  │ Action           │  │
│  │              │──▶│ Client       │──▶│ Dispatcher       │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## Orchestrator → Worker Contract

### Dispatch

When `Orchestrator._dispatch_ready_tasks()` claims a task, it:
1. Moves task from `pending/` → `doing/`
2. Returns (doesn't spawn worker - that's the worker's job)

The worker must:
1. Call `task_store.get_task(project, task_id)` to get current state
2. Execute the task
3. Move task to appropriate state (`done/`, `failed/`, `waiting/`)
4. Log metrics via `metrics.append(event)`

### Task Context

The worker receives:
```python
@dataclass
class TaskContext:
    task: Task                    # Current task
    project: str                 # Project name
    config: Config               # Budget limits
    vault_root: Path            # Vault root path
    changed_paths: dict          # From changeset (for retries)
```

### Task Lifecycle

```
pending/ ──dispatch──▶ doing/ ──complete──▶ done/
                        │
                        ├──timeout──▶ waiting/ ──retry──▶ doing/
                        │
                        └──error──▶ failed/
```

## Worker → Orchestrator Contract

### Worker Lifecycle

1. **Claim**: Read task from `doing/`
2. **Load Context**: Get changed_paths from `orchestrator.load_changeset()`
3. **Execute**: Run agent loop
4. **Track**: Call `orchestrator.track_write()` for each file change
5. **Complete**: Move task to final state
6. **Save**: Call `orchestrator.save_changeset()`
7. **Log**: Append metrics event

### Agent Loop Interface

```python
async def run_task(context: TaskContext) -> TaskResult:
    """
    Run a task until done, failed, or timeout.
    
    Returns:
        TaskResult with outcome and transcript
    """
```

### Action Dispatch Interface

The worker dispatches actions via `AgentDispatcher`:

```python
class AgentDispatcher:
    def dispatch(self, action: Action, context: TaskContext) -> ActionResult:
        """
        Dispatch an action to the sandbox.
        
        Validates action against tier (coding/plan).
        Returns result for transcript.
        """
```

## Provider Integration

### Worker Configuration

Workers are configured in `config.json`:

```json
{
  "workers": [
    {
      "name": "groq-fast",
      "model": "llama-3.3-70b-versatile",
      "base_url": "https://api.groq.com/openai/v1",
      "api_key": "...",
      "task_types": ["coding"]
    }
  ]
}
```

### Provider Client Interface

```python
class ProviderClient(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        model: str,
        max_tokens: int,
    ) -> str:
        """Return completion text."""
```

## Sandbox Integration

### Execute Command

```python
result = orchestrator.execute_command(
    command="python script.py",
    cwd=project_path,
    tier="coding",  # or "plan"
    timeout=30,
)
```

Returns `SpawnResult`:
```python
@dataclass
class SpawnResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    overhead_ms: float
```

### Snapshot on First Execute

Before first `execute` call, worker must:
```python
snapshot_path = orchestrator.ensure_snapshot(task.id, attempt, project_path)
```

## Metrics

### Task Event Format

```python
TaskEvent.create(
    task_id="task-01",
    task_type="coding",
    turns_used=3,
    max_turns=6,
    outcome="done",
    files_touched=5,
    changeset_from_attempt=2,
)
```

## Error Handling

### Unhandled Exception Safety

The orchestrator catches all exceptions in dispatch:
```python
try:
    worker.run_task(context)
except Exception as e:
    # Log error
    metrics.append(TaskEvent(..., outcome="failed"))
    # Move to failed/ or requeue
```

### Retry Logic

- Attempt 1: normal execution
- Attempt 2+: with rollback to snapshot if crash detected
- Max attempts: configurable per budget

## Files to Implement

1. **Agent Loop** (`lean/agent_loop.py`)
   - `run_task(context) -> TaskResult`
   - Turn management
   - Transcript handling

2. **Provider Client** (`lean/provider.py`)
   - `GroqProvider`, `OpenRouterProvider`
   - Rate limiting
   - Error handling

3. **Action Dispatcher** (`lean/dispatcher.py`)
   - `dispatch(action, context) -> ActionResult`
   - Read/write/execute validation
   - Sandbox integration

4. **Worker Thread** (`lean/worker.py`)
   - Thread management
   - Task claiming
   - Lifecycle handling

## Testing

Integration test at `tests/integration/test_minimal.py` passes.

For internals, add:
- `tests/integration/test_agent_loop.py`
- `tests/integration/test_provider_client.py`
- `tests/integration/test_action_dispatcher.py`

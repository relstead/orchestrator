# Vault Orchestrator

AI-powered task orchestration with multi-agent architecture.

## Features

- **Multi-Agent System**: Planner, Executor, and Reviewer agents work together
- **Git Workflow**: Branch, commit, and rollback support
- **Verification Pipeline**: Lint, type check, and test execution
- **Sandbox Execution**: Isolated code execution with resource limits
- **Live Dashboard**: Real-time monitoring of jobs and workers
- **Debugging**: Full execution traces and metrics

## Installation

```bash
# Clone the repository
git clone https://github.com/openhands/vault-orchestrator
cd vault-orchestrator

# Install
pip install -e .

# Or with dev dependencies
pip install -e ".[dev]"
```

## Quick Start

### 1. Create a Vault

```bash
vault-orchestrator init ~/my-vault
cd ~/my-vault
```

### 2. Configure Workers

Edit `config.json`:

```json
{
  "workers": [
    {
      "name": "gpt-4",
      "model": "gpt-4",
      "base_url": "https://api.openai.com/v1",
      "api_key": "your-key-here",
      "task_types": ["coding", "general"]
    }
  ]
}
```

### 3. Create a Project

```bash
vault-orchestrator new-project MyProject
```

### 4. Add a Task

Create `Projects/MyProject/tasks/pending/fix_bug.md`:

```markdown
Fix the login bug where users get logged out after 5 minutes.

Acceptance:
- All tests pass
- No lint errors
```

### 5. Run

```bash
vault-orchestrator run
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   MultiAgentOrchestrator                     │
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐ │
│  │   PLANNER    │───▶│  EXECUTOR    │───▶│  REVIEWER   │ │
│  └──────────────┘    └──────────────┘    └──────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## Task Lifecycle

```
PENDING → EXPLORED → PLANNED → EXECUTING → VERIFYING
                                      ↓            ↓
                                   REVISION ←──────┘
                                      ↓
                                  COMPLETED
```

## Project Structure

```
vault/
├── config.json           # Configuration
├── Projects/
│   └── MyProject/
│       ├── tasks/
│       │   ├── pending/   # New tasks
│       │   ├── doing/    # Currently running
│       │   └── done/     # Completed tasks
│       └── (project files)
└── _logs/               # Execution logs
```

## CLI Commands

```bash
# Initialize a new vault
vault-orchestrator init <path>

# Run the orchestrator
vault-orchestrator run

# Create a new project
vault-orchestrator new-project <name>

# List pending tasks
vault-orchestrator list

# View status
vault-orchestrator status
```

## API Usage

```python
from vault_orchestrator import (
    WorkerPool, JobStore, LiveDashboard, Tracer
)

# Initialize
worker_pool = WorkerPool(config)
job_store = JobStore(vault_root)
dashboard = LiveDashboard(log_fn=print)

# Process a job
job = job_store.enqueue("Projects/MyProject/task.md", "coding", "MyProject")
worker = worker_pool.get_available("coding")
job_store.assign(job, worker)

# Track with tracer
tracer = Tracer(job.id, task_description)
tracer.reasoning("Starting task", confidence=0.9)

# Run executor...
```

## Development

```bash
# Run tests
pytest vault_orchestrator/tests/

# Lint
ruff check vault_orchestrator/

# Type check
mypy vault_orchestrator/

# Format
ruff format vault_orchestrator/
```

## License

MIT

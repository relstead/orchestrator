# Lean Vault Orchestrator

Hybrid architecture: deterministic code handles orchestration, AI handles inference only.

## Principles

1. **AI performs inference only** - Code writes, AI decides
2. **Deterministic infrastructure** - DAG, indexing, verification are code
3. **Multiple workers** - Parallel execution with dependency constraints
4. **Bubbles** - Each worker isolated until verified

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      DETERMINISTIC CODE                           │
│                                                                  │
│  Dependency Graph ──→ Ready Queue ──→ Worker Dispatch         │
│  Code Indexer ────→ Relevant Files ──→ Context                 │
│  Verification Pipeline (pytest) ──→ Pass/Fail                   │
│  Rollback Manager ───→ Snapshot/Restore                        │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                      AI WORKERS                                  │
│                                                                  │
│  Worker 1: task + context → read/write/execute → verified       │
│  Worker 2: task + context → read/write/execute → verified       │
│  Worker N: ...                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## What Code Does

| Component | Responsibility |
|-----------|----------------|
| `vault.py` | Path containment, security |
| `tasks.py` | Task lifecycle, claim/release |
| `worker.py` | Worker state, metrics, cooldown |
| `dependency.py` | DAG from task deps, cycle detection |
| `indexer.py` | File relevance search |
| `verification.py` | pytest execution |
| `rollback.py` | Snapshot/restore |
| `sandbox.py` | Command isolation |
| `orchestrator.py` | Worker pool, task dispatch |

## What AI Does

- Reads pre-selected relevant files
- Writes code
- Executes commands
- Decides how to fix issues

## Usage

```bash
# Install
pip install -e .

# Initialize vault
lean init ~/vault

# Add API key to config.json
vim ~/vault/config.json

# Add tasks
lean add ~/vault "Fix login bug" auth
lean add ~/vault "Add tests" --depends-on auth

# Run
lean run ~/vault
```

## Task Format

Tasks are markdown files:

```markdown
<!-- meta: type=coding attempts=0 -->

Fix the login timeout bug.

depends-on: task-xxx
```

## Files

```
lean/
├── __init__.py       # Package
├── agent.py          # AI interface (80 lines)
├── cli.py            # Entry point (140 lines)
├── config.py         # Config (40 lines)
├── dependency.py    # DAG (160 lines)
├── indexer.py        # Relevance (180 lines)
├── orchestrator.py   # Main loop (350 lines)
├── rollback.py       # Snapshot (70 lines)
├── sandbox.py        # Isolation (60 lines)
├── tasks.py          # Task lifecycle (140 lines)
├── vault.py          # Security (70 lines)
├── verification.py  # Tests (130 lines)
├── worker.py        # Worker state (200 lines)
└── pyproject.toml

Total: ~1620 lines
```

## vs Original

| Metric | Original | Lean |
|--------|----------|------|
| Lines | ~15,000 | ~1,620 |
| AI role | Multi-agent | Inference only |
| Planner/Reviewer/Supervisor | Yes | No |
| DAG | Complex | Simple |
| Verification | AI + tools | Tools only |
| Full metrics | Partial | Yes |
| Multi-worker | Yes | Yes |

# Implementation Audit Trail

This document tracks all changes made against the Vault Orchestrator specification. Each entry documents the change, which spec section it addresses, and the rationale.

---

## Commit: `11c4ef0` - Initial Implementation

### Changes
- Created `pyproject.toml` - Project configuration
- Created `lean/__init__.py` - Empty package init
- Created `lean/sandbox.py` - Job Object wrapper, Restricted Token, AppContainer, path containment, pipe capture
- Created `lean/self_test.py` - 48 self-tests from §15

### Spec Sections Addressed
- §0.5: Steps 1-5 (Job Object, Restricted Token, AppContainer, ACL, Pipe Capture)
- §8.1-8.3: Sandbox architecture
- §8.6: Path containment (defense-in-depth)
- §15: Self-test suite

### Notes
- Sandbox Python gates implemented (command allowlist/denylist)
- Platform-aware Windows API bindings with ctypes
- Tests stubbed for Windows-only features

---

## Commit: `104dd49` - Phase 1: Bedrock Validation

### Task 1.1: Fix Critical Dispatch Bug
**File:** `lean/orchestrator.py`
**Change:** Line 259: `TaskState.DONE` → `TaskState.DOING`
**Spec Section:** §6 (Task States)
**Rationale:** Bug: `_dispatch_ready_tasks()` was moving tasks directly to DONE instead of DOING, breaking the entire execution loop. Task must enter doing/ state before work begins.

### Task 1.2: Populate __init__.py
**File:** `lean/__init__.py`
**Change:** Added all package-level exports for: Config, Vault, Task/TaskMeta/TaskState/TaskStore/TaskType, DependencyGraph, Indexer, Action/ActionType, sandbox classes, logger classes, Worker, PlannerValidator, Verification, Orchestrator
**Spec Section:** §13 (Component List)
**Rationale:** Package-level imports must work for self-test suite and external consumers.

### Task 1.3: Validate Module Dependency Graph
**Files:** All `.py` files
**Change:** Verified import graph matches §13 topological build order. No circular imports detected.
**Spec Section:** §13
**Rationale:** Spec mandates: config → vault → tasks → dependency → indexer → agent → sandbox. Violations mean wrong build order.

### Task 1.4: Audit Config Schema Completeness
**File:** `lean/config.py`
**Change:** Verified all §12 fields present: workers[], budgets{} (11 fields), metrics{} (compact_at_bytes), use_planning
**Spec Section:** §12 (Config Schema)
**Rationale:** Missing budget knobs silently cause wrong planner behavior.

### Task 1.5: Verify Task State Machine Integrity
**File:** `lean/tasks.py`
**Change:** Verified TaskState enum has all 6 states (pending, doing, done, blocked, waiting, failed). Verified TaskMeta.parse() extracts all §5 fields.
**Spec Section:** §5, §6
**Rationale:** blocked vs failed semantics are load-bearing for dependency resolution.

### Task 1.6: Confirm Indexer Single-Owner Invariant
**File:** `lean/indexer.py`
**Change:** Verified `Indexer.get_index()` uses `project_path.resolve()` as dict key (not `str()`). Test confirms: `indexer.get_index(p) is indexer.get_index(p.resolve())`
**Spec Section:** §9.1
**Rationale:** §9.1 explicitly names dual-indexer bug from str() vs Path() keying.

### Task 1.7: Validate Sandbox Primitive Independence
**Files:** `lean/sandbox.py`
**Change:** Verified JobObject, RestrictedTokenSandbox, AppContainerSandbox can each be instantiated independently.
**Spec Section:** §0.5
**Rationale:** Steps 1-3 must each be testable in isolation before integration.

---

## Commit: `3e65716` - Phase 2: Fill Critical Gaps

### Task 2.1: ACL Setup (Step 4)
**File:** `lean/sandbox.py`
**Change:** Added `ACLSetup` class with:
- `grant_path_access(path, package_sid, read, write)` - Uses SetNamedSecurityInfo
- `setup_vault_paths(vault_root, package_sid, discovered_bins)` - Sets up vault, Projects/, Skills/, binaries
- `_setup_cache` - Prevents duplicate ACL operations
**Spec Section:** §0.5 Step 4
**Rationale:** Security-critical step. A bug here is a silent sandbox bypass.

### Task 2.4: Execute Snapshot
**File:** `lean/orchestrator.py`
**Change:** Added `ensure_snapshot(task_id, attempt, project_path)` and `rollback_to_snapshot(task_id)`.
- Copies project directory excluding tasks/ to `_backups/snapshot_<task_id>_<attempt>/`
- Returns cached path if snapshot already taken for task+attempt
**Spec Section:** §8.12
**Rationale:** Snapshot before first execute per attempt. Required for rollback and crash recovery.

### Task 2.5: Changeset Manifest Integration
**File:** `lean/orchestrator.py`
**Change:** Added:
- `track_write(path, status)` - Records path → {status, attempt, timestamp}
- `track_overwrite(path)` - Records "overwritten" status
- `save_changeset(task_id)` - Writes to `_backups/changeset_<task_id>.json`
- `load_changeset(task_id)` - Loads and accumulates across retries
**Spec Section:** §9.2
**Rationale:** Changeset handoff is load-bearing for multi-attempt tasks.

### Task 2.6: Output Compression
**File:** `lean/orchestrator.py`
**Change:** Added:
- `compress_output(raw, max_chars=4000)` - Returns head(20) + tail(10) summary if too long
- `save_full_output(task_id, turn, output)` - Saves to `_backups/execute_<task_id>_<turn>.txt`
**Spec Section:** §8.9
**Rationale:** Prevents transcript bloat while preserving full output.

### Task 2.7: Compaction Cap
**File:** `lean/orchestrator.py`
**Change:** Added `compaction_ran_this_cycle` flag to OrchestratorState. Modified `_poll()` to reset each cycle. Modified `_maybe_compact()` to check flag and prevent multiple compactions per cycle.
**Spec Section:** §9.4
**Rationale:** Several projects crossing threshold must not all fire at once.

### Task 2.8: Inbox Decomposition
**File:** `lean/orchestrator.py`
**Change:** Replaced stub `_process_inbox()` with real implementation:
- `_parse_inbox_items(content)` - Parses [project] and @project: syntax, --- separators
- `_infer_task_type(body)` - Heuristic for plan vs coding task type
- Creates tasks via TaskStore.create_task()
**Spec Section:** §9.5
**Rationale:** Inbox is primary task ingestion path.

---

## Commit: `e0c62b1` - Phase 3: Priming for Internals

### Task 3.1: Document Bedrock State
**File:** `BEDROCK_STATUS.md` (new)
**Change:** Created status document tracking:
- Build order completion (§0.5)
- Component status (§13)
- Self-test results
- Known limitations
**Spec Section:** All
**Rationale:** Prevents next contributor from rediscovering same gaps.

### Task 3.2: Minimal Integration Test
**File:** `tests/integration/test_minimal.py` (new)
**Change:** Created 10 integration tests covering:
- Vault/project creation
- Task CRUD and state transitions
- Dependency graph
- Snapshot and changeset
- Output compression
- Inbox parsing
- Task type inference
- Metrics writer
- Compaction cap
**Spec Section:** All
**Rationale:** Canary test for bedrock solidity.

### Task 3.3: Define Internals Interface Contract
**File:** `INTERNALS_CONTRACT.md` (new)
**Change:** Documented interface between bedrock and internals:
- Orchestrator → Worker contract (dispatch, task context, lifecycle)
- Worker → Orchestrator contract (execute, track, complete)
- Provider integration interface
- Sandbox integration
- Metrics format
**Spec Section:** §10, §13
**Rationale:** Makes boundary explicit, prevents architectural drift.

### Task 3.4: Tag Release
**Change:** Created tag `bedrock-v1`
**Spec Section:** N/A
**Rationale:** Known-good checkpoint for internals work.

---

## Bug Fixes (Non-Spec Changes)

### TaskStore.get_tasks_in_state() Accepts String
**File:** `lean/tasks.py`
**Change:** Modified `get_tasks_in_state()` to accept `TaskState | str` instead of just `TaskState`. Extracts `.value` if enum, uses string directly.
**Rationale:** Test compatibility. Orchestrator passes strings for states in some places.

---

## Pending (Not Yet Implemented)

Per spec, the following are NOT yet implemented (internals work):

1. **Agent Loop** - Full turn execution, LLM calls, action dispatch
2. **Provider Integration** - HTTP client for Groq/OpenRouter/etc.
3. **Real Sandbox Spawn** - AppContainer process creation with SECURITY_CAPABILITIES
4. **Task Transcript** - Full transcript management during task execution
5. **Rollback on Crash** - Recovery from crashes mid-task
6. **Model-Driven Inbox Decomposition** - Uses simple parser, not model

These are tracked in `INTERNALS_CONTRACT.md` as the starting point for internals work.

---

## Verification Commands

```bash
# Self-tests
python -m lean.self_test

# Integration tests
python -m pytest tests/integration/test_minimal.py -v

# All tests
python -m pytest tests/ -v
```

## Git References

- `bedrock-v1` tag: Points to Phase 2 completion (all bedrock tasks done)
- `main` branch: Latest state including Phase 3 documentation

# Vault Orchestrator — Spec

This is the canonical description of what this app is and does. Any change —
by any agent, any session, any fork — should be checked against this
document before being called done. If a change contradicts something here,
either the change is wrong or this doc is stale; update whichever one is
actually true, on purpose, not by drift.

**What this is:** a lightweight sub-4k code desktop app that lets free-tier / open
API models work semi-autonomously on multiple projects against an Obsidian vault, using the
vault's own files as the agent's memory — not a side database, not a
separate context store. 

**What it is explicitly not trying to be:**
- Not built for paid/proprietary models specifically — free-tier providers
  (Groq, OpenRouter free models, Gemini Flash, Cerebras) are the primary
  target, and the budget-consciousness (rate-limit backoff, compaction,
  capped turns) is a first-class design constraint, not an afterthought. 

---

## User Workflow

This section describes the intended user experience, from installation to ongoing use.

### 1. Installation & Setup

```bash
# Install the app
pip install lean  # or: python -m pip install lean

# Initialize a vault (creates folder structure)
lean init ~/my-vault
```

### 2. Configure API Keys

Edit `~/my-vault/config.json` to add API keys:

```json
{
  "workers": [
    {
      "name": "groq-free",
      "model": "llama-3.1-8b-instant",
      "base_url": "https://api.groq.com/openai/v1",
      "api_key": "gsk_xxxxx"
    }
  ]
}
```

**Free tier providers** (no API key required or free tier available):
- Groq (free tier with Llama models)
- OpenRouter (free models available)
- OpenAI (free trial)
- Anthropic (free trial)

### 3. Start the Orchestrator

```bash
# Run the orchestrator (stays in background)
lean run ~/my-vault

# Check status
lean status ~/my-vault
```

The orchestrator will:
- Auto-create vault structure if not present
- Scan for tasks every second
- Respect rate limits (429 = cooldown, 401/403 = disable)
- Dispatch tasks to available workers in parallel

### 4. Open in Obsidian

1. Open Obsidian
2. "Open folder as vault"
3. Select `~/my-vault`
4. See the vault structure in the file explorer

### 5. Add Tasks (No AI Required)

**Option A: Add via inbox (auto-converted to tasks)**
```markdown
# In _inbox.md
Fix the login bug
Add dark mode support
Update documentation
```
*Note: Inbox processing is planned but not yet implemented.*

**Option B: Add via `lean add` CLI**
```bash
lean add ~/my-vault "Fix the login bug" my-project
```

**Option C: Manual task file**
Create `Projects/my-project/tasks/pending/my-task.md`:
```markdown
Fix the login bug

Fix the authentication flow that's rejecting valid credentials.
```

### 6. Automatic Project Detection

When you create a new folder under `Projects/`:
```
Projects/
  new-project/   ← Created manually or by Obsidian
```

The orchestrator detects this on the next scan and auto-populates:
```
Projects/new-project/
  assets/
  NOTES.md
  STATUS.md
  tasks/
    pending/
    doing/
    done/
    blocked/
    waiting/
```

*Note: Auto-detection is planned but not yet implemented. Use `lean add` or create files manually.*

### 7. Task Lifecycle (Visible in Obsidian)

| Folder | Meaning |
|--------|---------|
| `pending/` | Waiting to be processed |
| `doing/` | Currently being worked on by a worker |
| `done/` | Successfully completed |
| `failed/` | Failed after max attempts |
| `waiting/` | Blocked waiting for human input |
| `blocked/` | Dependency not satisfied |

You can drag files between folders manually — the app will respect external changes.

### 8. Monitor Progress

```bash
lean status ~/my-vault
# Output:
# Vault: /home/user/my-vault
# Tasks:
#   Pending: 5
#   Running: 2
#   Done: 12
#   Failed: 1
```

---

## Architectural Invariants

The following principles are expected to remain true regardless of future features.

1. **The vault is the source of truth.** All persistent state lives as human-readable files.

2. **The application owns system state.** The orchestrator discovers, tracks, validates and persists state. The AI never becomes responsible for remembering or reconstructing it.

3. **The AI performs inference only.** Models are used where reasoning is required. Deterministic software performs orchestration, validation, scheduling and recovery.

4. **Every feature should make the filesystem more understandable, not more hidden.**

5. **Prefer fixing observed failures over introducing speculative architecture.**

---

## Vault Architecture

```
<vault root>/
  _active.md             # plain text: name of the project with current focus
  _inbox.md              # freeform notes -- turned into tasks automatically
  _inbox_archive.md      # processed inbox content, appended with timestamps
  _digest.md             # short human-facing "what happened" feed, whole vault
  _backups/              # per-file backups (before overwrite) + pre-execute snapshots
  _archive/<project>/    # compacted STATUS.md history, archived done/ tasks
  _archive/_digest/      # compacted _digest.md history
  Skills/<name>/SKILL.md # optional: house conventions, auto-surfaced to agents
  Projects/<name>/
    assets/
    NOTES.md
    STATUS.md             # append-only human-facing log, auto-compacted when large
    tasks/
      pending/    doing/    done/    blocked/    waiting/
```

A task's **folder location is its state** — moving the file is the state
transition. This is deliberate: it's inspectable and editable by a human in
Obsidian directly, with no separate UI required to understand or intervene.

- `pending` → claimed into `doing` → resolves to `done`, `blocked`,
  back to `pending` (retry), or `waiting` (needs a human answer).
- A human can drag a file between these folders by hand at any time; the
  app never assumes it's the only thing touching the vault.

---

## Agent Loop

One task = one call to `_process_task`, which runs up to `max_turns`
(default 6) turns. Each turn, the model must reply with **exactly one JSON
object**:

```json
{"reasoning": "<1-2 sentences, before the action>",
 "action": "list" | "read" | "write" | "execute" | "ask_human" | "final",
 ...action-specific fields...}
```

| Action | Scope | Notes |
|--------|-------|-------|
| `list` | whole vault | shallow map auto-injected each turn |
| `read` | whole vault | binary/non-UTF8 files fail informatively |
| `write` | current project only | full-file replace; backed up first |
| `execute` | current project's cwd | sandboxed, opt-in |
| `ask_human` | — | parks task in `tasks/waiting/` |
| `final` | — | ends the attempt |

**AI does not discover system state independently.**
**The application provides the current state.**
**The AI may request additional files when needed to perform inference.**

---

## Safety Guardrails

1. **Path containment**: every read/list/write resolves through `safe_vault_path`; nothing outside the vault root is reachable.

2. **Write scope**: `write` is refused outside the current project folder.

3. **Backup before overwrite**: any file `write` overwrites gets copied to `_backups/` first, timestamped.

4. **Read-before-write nudge**: overwriting a file not `read` this attempt is refused once per path.

5. **Execute** (opt-in, off by default):
   - Denylist against catastrophic patterns (`rm -rf /`, `sudo`, fork bombs, pipe-to-shell, etc.)
   - cwd locked to the current project folder
   - Timeout-bounded, output-capped
   - Full project snapshot before first `execute` call per attempt

---

## Crash Resilience

- `persist_turn_progress` writes the transcript to disk **after every turn**.
- `sweep_stale_claims` runs **every poll cycle** — tasks orphaned mid-session are recovered within one poll interval.
- Crash-recovery **charges an attempt**.
- User pause/stop does **not** charge an attempt**.
- Unhandled exceptions inside a single action's handling are caught and turned into an `ERROR` observation.

---

## Provider Pool

- Multiple providers, each declaring which `task_types` (`general`, `reasoning`, `coding`) they serve.
- 429 → short cooldown (respects `Retry-After`). 401/402/403 → long cooldown.
- Settings changes update the pool **in place**, not a new instance.

---

## Self-Test Suite

Runs against a throwaway temp vault, never the user's real one. Runs automatically on startup.

Current coverage:
1. Vault sandboxing blocks path traversal
2. Parser handles braces inside written content
3. Write action refuses paths outside project
4. Crash mid-task preserves progress on recovery
5. Execute action blocks destructive commands
6. Execute action respects its timeout
7. Settings save preserves provider cooldowns

---

## Explicit Non-Goals

- Multi-instance coordination beyond crash recovery
- True sandboxing / containerized execution
- Git-native workflow (branch-per-task, auto-PR)
- A lint/typecheck/test/security verification pipeline assuming a full installed toolchain
- A task scheduler with priority/dependency graphs and parallel workers (see simplified branch)
- A web dashboard

---

## Process Rule

Before saying a change is done:

1. Did you re-read the file from disk after editing it?
2. Does a self-test exist that would fail if this regressed? If not, add one.
3. Run the self-test suite. All of it.
4. If extending scope, check against Non-Goals first.

---

## Implementation: Lean Branch

See `lean/` directory for the production implementation:

- **vault.py**: Path containment (from main)
- **tasks.py**: Task lifecycle (from main)
- **worker.py**: Full metrics, cooldown, status (from main)
- **dependency.py**: DAG from task deps (hybrid)
- **indexer.py**: Code-based relevance search (hybrid)
- **verification.py**: pytest execution (hybrid)
- **rollback.py**: Snapshot/restore (hybrid)
- **sandbox.py**: Command isolation (hybrid)
- **agent.py**: Minimal AI interface (new)
- **orchestrator.py**: DAG + Worker Pool + Dispatch (new)

**~1,800 lines total** - under the 5k budget.

Key decisions:
- Multiple workers allowed (parallel execution)
- Full metrics from main (tokens, duration, success rate)
- In-memory job tracking with file persistence for crash recovery
- DAG built from `depends-on:` fields (not AI hallucinated)
- Verification is code (pytest), not AI reviewer

---

## Implementation Status

### ✅ Implemented

| Feature | Status | Location |
|---------|--------|----------|
| Vault structure creation | ✅ | `vault.py: ensure_vault_skeleton()` |
| Task lifecycle (pending→doing→done) | ✅ | `tasks.py` |
| Parallel task dispatch | ✅ | `orchestrator.py: _run()` |
| Worker pool with rate limit handling | ✅ | `worker.py` |
| Path containment (security) | ✅ | `vault.py: safe_vault_path()` |
| Command sandboxing | ✅ | `sandbox.py` |
| Snapshot/rollback | ✅ | `rollback.py` |
| Dependency graph (DAG) | ✅ | `dependency.py` |
| Code relevance search | ✅ | `indexer.py` |
| pytest verification | ✅ | `verification.py` |
| Self-test suite | ✅ | `self_test.py` |
| CLI (init, run, status, add) | ✅ | `cli.py` |

### 🔄 Planned (Not Yet Implemented)

| Feature | Description |
|---------|-------------|
| Inbox processing | Auto-convert `_inbox.md` entries to tasks |
| Auto-project detection | Detect new folders under `Projects/` and auto-populate |
| Digest generation | Auto-update `_digest.md` with activity feed |
| STATUS.md compaction | Auto-compact large STATUS.md files |
| Configuration UI | GUI for editing config without JSON |
| Background service | Proper daemon/service mode |

### 🔧 How to Implement Missing Features

**Inbox Processing:**
1. Add `process_inbox()` function in `orchestrator.py`
2. Scan `_inbox.md` for new lines (vs `_inbox_archive.md`)
3. Create task files for each new line
4. Append to `_inbox_archive.md` with timestamp

**Auto-Project Detection:**
1. Add `detect_new_projects()` in `orchestrator.py`
2. On each scan, compare `Projects/` folder list to known projects
3. Call `ensure_project_skeleton()` for new folders
4. Store known projects in `_active.md` or separate tracking file

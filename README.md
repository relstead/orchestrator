# Vault Orchestrator

Windows-native sandboxed AI task runner for Obsidian vaults.

## Status

Pre-implementation for §6 onward (sandbox integration). Foundation modules and sandbox core are built and tested.

## Quick Start

```bash
# Initialize a vault
lean init ./my-vault

# Run the orchestrator
lean run ./my-vault

# Check status
lean status ./my-vault

# Add a task
lean add ./my-vault "Fix the login bug" my-project
```

## Architecture

Three-layer sandbox architecture (§8.2):
1. **Python Gates** - Defense-in-depth command validation
2. **Job Object** - Resource control (kill-on-close, process limits)
3. **Kernel Enforcement** - AppContainer (primary) or Restricted Token (fallback)

## Components

| Module | Purpose |
|--------|---------|
| `config.py` | Configuration management |
| `vault.py` | Vault path handling and skeleton |
| `tasks.py` | Task file parsing and state management |
| `dependency.py` | Dependency graph and cycle detection |
| `indexer.py` | Single-owner project index |
| `agent.py` | Action parsing and string-aware JSON extraction |
| `sandbox.py` | Windows-native kernel-enforced sandbox |
| `logger.py` | Append-only metrics and digest logging |
| `worker.py` | Provider pool with rate limiting |
| `planner.py` | Plan validation (no provider calls) |
| `orchestrator.py` | Main loop and dispatch |
| `cli.py` | Command-line interface |
| `self_test.py` | Self-test suite (40 tests) |

## Self-Tests

```bash
python -m lean.self_test
```

## Build Order

Per §0.5:
1. Job Object Wrapper
2. Restricted Token + Low Integrity
3. AppContainer Profile Creation
4. ACL Setup on Vault Paths
5. Pipe-Based stdout/stderr Capture
6. Full Integration with Agent Loop

## Platform

Windows only. Uses native Windows APIs (ctypes) - no third-party tools, no WSL, no Docker.

## References

- [SPEC.md](./SPEC.md) - Canonical specification (v1)
- [VAULT_ORCHESTRATOR_SPEC.md](./VAULT_ORCHESTRATOR_SPEC.md) - v3 spec (external)
- [CHANGESET_MANIFEST.md](./CHANGESET_MANIFEST.md) - Cross-attempt handoff pattern

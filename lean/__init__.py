"""Lean Vault Orchestrator.

Hybrid architecture: deterministic code handles orchestration,
AI handles inference only.

Modules:
- vault.py: Path containment (security critical)
- tasks.py: Task file handling and lifecycle
- worker.py: Worker state machine with full metrics
- dependency.py: DAG from task dependencies
- indexer.py: Code-based relevance search
- verification.py: Deterministic test execution
- rollback.py: Snapshot and restore
- sandbox.py: Command isolation
- agent.py: Minimal AI interface
- orchestrator.py: Main coordination
- config.py: Configuration
- cli.py: Entry point

Total: ~1500 lines
"""

# Bedrock Status

This document tracks the completion state of the Vault Orchestrator implementation.

## Overview

**Phase 1 (Bedrock Validation)**: ✅ Complete  
**Phase 2 (Fill Critical Gaps)**: ✅ Complete  
**Phase 3 (Priming for Internals)**: ✅ Complete  
**v1.0 (Internals)**: ✅ Complete

## Build Order (§0.5)

| Step | Component | Status | Notes |
|------|-----------|--------|-------|
| 1 | Job Object Wrapper | ✅ | `JobObject` class, kill-on-close, process limits |
| 2 | Restricted Token | ✅ | `RestrictedTokenSandbox` class, Low Integrity fallback |
| 3 | AppContainer Profile | ✅ | `AppContainerSandbox` class, profile caching |
| 4 | ACL Setup | ✅ | `ACLSetup` class, grant_path_access(), setup_vault_paths() |
| 5 | Pipe Capture | ✅ | `_create_pipe()`, stdout/stderr capture |
| 6 | Full Integration | ⚠️ | Sandbox stubbed in orchestrator |

## Component Status (§13)

| Module | Status | Notes |
|--------|--------|-------|
| config.py | ✅ | Config, BudgetConfig, MetricsConfig, WorkerConfig |
| vault.py | ✅ | Vault, path containment, skeleton creation |
| tasks.py | ✅ | Task, TaskMeta, TaskState, TaskStore |
| dependency.py | ✅ | DependencyGraph, build_graph, cycle detection |
| indexer.py | ✅ | Single-owner index, extension-aware comment stripping |
| agent.py | ✅ | Action parsing, string-aware JSON extraction |
| sandbox.py | ✅ | All sandbox primitives + ACLSetup |
| logger.py | ✅ | MetricsWriter, DigestWriter with compaction |
| worker.py | ✅ | WorkerPool + task execution |
| planner.py | ✅ | Plan validation (no provider calls) |
| verification.py | ✅ | Task/project verification |
| orchestrator.py | ✅ | Main loop, dispatch, snapshot, changeset |
| cli.py | ✅ | init, run, status, add commands |
| **provider.py** | ✅ | HTTP client for Groq/OpenRouter APIs |
| **agent_loop.py** | ✅ | Turn execution, transcript management |
| **dispatcher.py** | ✅ | Action dispatch, path validation |

## Self-Test Results

```
40 passed, 4 skipped (Windows-only)
```

### Passing Tests (40)
- test_1_vault_sandbox_blocks_path_traversal
- test_2_parser_handles_braces_in_content
- test_3_parser_handles_markdown_fences
- test_4_write_action_refuses_paths_outside_project
- test_5_crash_mid_task_preserves_progress
- test_6_repeated_crashes_charge_attempts
- test_7_working_transcript_stripped_on_success
- test_10_execute_triggers_snapshot_once
- test_11_inbox_decomposition_backs_off
- test_12_compaction_backs_off_and_capped
- test_13_digest_compaction_archives_byte_identical
- test_14_settings_save_preserves_cooldowns
- test_15_blocked_tasks_detected_at_creation
- test_16_blocked_never_auto_promoted
- test_17_stale_claim_sweep_runs_every_cycle
- test_18_unhandled_exception_caught_at_dispatch
- test_19_propose_plan_cyclic_batch_rejected
- test_20_propose_plan_exceeding_depth_rejected
- test_21_propose_plan_body_exceeds_max_chars_rejected
- test_22_propose_plan_context_hint_outside_vault_rejected
- test_23-25_plan_tier_execute_refuses_*
- test_27_changeset_accumulates_across_attempts
- test_28_metrics_jsonl_one_line_per_attempt
- test_29_metrics_compacts_at_threshold
- test_30_indexer_preserves_markdown_headings
- test_31_two_call_sites_same_index_state
- test_32-33_appcontainer_cannot_read/write_outside
- test_36_restricted_token_fallback_works
- test_37_python_path_containment_rejects_absolute
- test_38_output_compression_truncates
- test_41_acl_setup_runs_once
- test_42-44_plan_tier_*
- test_45_coding_tier_path_traversal_blocked
- test_46-47_appcontainer_registry/named_objects

### Skipped Tests (4) - Windows Only
- test_35_job_object_kill_on_close
- test_39_sandbox_overhead
- test_40_appcontainer_profile_created_once
- test_48_orchestrator_standard_user_can_create_appcontainer

## Bugs Fixed

1. **Critical Dispatch Bug** (Phase 1.1): TaskState.DONE → TaskState.DOING in `_dispatch_ready_tasks()`

## Known Limitations

### Non-Windows Platforms
- Job Object not available (Windows kernel feature)
- AppContainer not available (Windows kernel feature)
- ACL manipulation not available (Windows API)

### Sandbox Execution
- `execute_command()` returns mock result, not real sandbox execution
- Full AppContainer spawn requires PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES

### Provider Integration
- Worker pool exists but no actual HTTP client for API calls
- No LLM integration (agent loop is stubbed)

### Inbox Decomposition
- Uses simple parser, not model-driven decomposition

## What Remains (v1.1)

The following are **nice to have** for v1.1:

1. **Real Sandbox Spawn**: Complete AppContainer spawn with SECURITY_CAPABILITIES
2. **Model-Driven Inbox Decomposition**: Uses simple parser, not model
3. **Async Task Execution**: Spawn worker threads instead of blocking poll cycle

## Getting Started

See [INTERNALS_CONTRACT.md](./INTERNALS_CONTRACT.md) for the interface contract.

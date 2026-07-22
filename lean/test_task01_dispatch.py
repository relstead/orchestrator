"""
Self-test stub for TASK-01: worker dispatch/inference mismatch.

Bug: _run() reserves a worker via pool.get_idle() + worker.start_job()
(marks it BUSY), but _agent_loop() calls self.pool.call(), which
independently re-derives candidates via get_available() -- excluding
the worker that was just marked busy. In a single-worker config this
means every task fails immediately with "No available workers".

This file is written to FAIL against the current orchestrator.py and
should PASS once TASK-01 is fixed (the reserved Worker object is
threaded through to the actual inference call).

Run with: python -m lean.test_task01_dispatch
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from lean.orchestrator import Orchestrator
from lean.vault import ensure_project_skeleton, new_task_file


def fake_chat_completion(*args, **kwargs):
    """Stand in for requests.post to the /chat/completions endpoint.

    Returns a valid 'final' action immediately so the agent loop
    terminates in one turn if it ever gets to call the model at all.
    """
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": json.dumps({
                "reasoning": "done",
                "action": "final",
                "result": "no-op success",
            })},
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    resp.raise_for_status = lambda: None
    resp.headers = {}
    return resp


def run_test() -> bool:
    vault = Path(tempfile.mkdtemp(prefix="task01_test_"))
    passed = True
    try:
        # Single-worker config -- this is the documented common case
        # (every example in SPEC.md/README.md configures exactly one worker).
        config = {
            "workers": [{
                "name": "solo-worker",
                "model": "llama-3.1-8b-instant",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key": "test-key",
                "task_types": ["general"],
            }]
        }
        (vault / "config.json").write_text(json.dumps(config), encoding="utf-8")

        # NOTE: ensure_vault_skeleton() does not create _audit.md, but
        # AuditLog.log() unconditionally read_text()s it on first use.
        # That's a separate bug (fresh vault -> FileNotFoundError on the
        # very first task) -- worked around here so this test isolates
        # TASK-01 specifically. Track it as TASK-11.
        (vault / "_audit.md").write_text("", encoding="utf-8")

        orch = Orchestrator(vault)
        project_path = ensure_project_skeleton(vault, "demo-project")
        task_path = new_task_file(project_path, "general", "Do a trivial no-op task.")

        with patch("requests.post", side_effect=fake_chat_completion):
            # Reproduce exactly what _run()'s dispatch loop does:
            # reserve a worker, mark it busy, then execute the task
            # the same way _execute_and_release does.
            worker = orch.pool.get_idle("general")
            assert worker is not None, "setup problem: no idle worker found"
            worker.start_job(task_path.stem)

            result = orch.execute_task(task_path)

        if not result.success:
            print(f"FAIL: task did not succeed. output={result.output!r}")
            passed = False
        else:
            print("PASS: single-worker dispatch completed without "
                  "'No available workers'")

        # Bonus check: assert the SAME worker object that was reserved is the
        # one whose metrics reflect the job (not left dangling BUSY forever,
        # and not a phantom second worker doing invisible work).
        if worker.current_job_id is not None:
            print(f"FAIL: reserved worker still shows current_job_id="
                  f"{worker.current_job_id!r} after task finished "
                  f"(finish_job was never called on it)")
            passed = False
        else:
            print("PASS: reserved worker's job slot was released correctly")

    finally:
        shutil.rmtree(vault, ignore_errors=True)

    return passed


if __name__ == "__main__":
    ok = run_test()
    print()
    print("TASK-01 dispatch test:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)

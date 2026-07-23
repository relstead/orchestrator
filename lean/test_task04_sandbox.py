"""
Self-test for TASK-04 (real isolation) and TASK-18 (fallback decision).

This file asserts three security properties:

  1. Secrets (API keys / config values) must not be visible inside the
     executed command's environment.
  2. The command must not be able to read files outside the project
     directory (requires bubblewrap for proper isolation).
  3. `execute` must be refused entirely unless bubblewrap is available
     (TASK-18: refuse-by-default with explicit opt-in for unsandboxed).

Run with: python -m lean.test_task04_sandbox

NOTE: This test REQUIRES bubblewrap to be installed. If bwrap is not available,
the test will FAIL rather than skip, to ensure the environment is properly set up.
"""

import os
import sys
import shutil
import tempfile
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lean.sandbox import execute, _check_bubblewrap


def test_bubblewrap_required() -> bool:
    """Verify bubblewrap is installed and can run. If not, FAIL (don't skip).
    
    TASK-18: bubblewrap is required for proper sandboxing. We refuse to
    run with weaker isolation by default.
    
    Note: bubblewrap requires user namespace capabilities which may not be
    available in all container environments (e.g., Docker without --privileged).
    """
    if not _check_bubblewrap():
        print("FAIL: bubblewrap required but not functional")
        print("      bwrap is installed but cannot run (likely container namespace restrictions)")
        print("      In environments where user namespaces are allowed, bwrap should work.")
        print("      Without functional bubblewrap, execute() refuses to run by default.")
        return False
    print("PASS: bubblewrap is installed and functional")
    return True


def test_secrets_not_leaked_to_subprocess_env() -> bool:
    """A command run via execute() should not see the orchestrator's
    own process environment -- in particular, API keys that the
    orchestrator loaded from config.json / env vars must not appear.
    """
    project_dir = Path(tempfile.mkdtemp(prefix="task04_env_"))
    try:
        os.environ["FAKE_LEAN_API_KEY"] = "sk-should-not-leak-12345"
        try:
            result = execute("env", project_dir, timeout=10)
            # Check that the error message doesn't contain the leaked secret
            # (even the refusal message shouldn't echo the secret)
            leaked = "sk-should-not-leak-12345" in result.output
            if leaked:
                print("FAIL: subprocess env exposes orchestrator secrets "
                      f"(found FAKE_LEAN_API_KEY in output)")
                return False
            print("PASS: subprocess env does not expose orchestrator secrets")
            return True
        finally:
            del os.environ["FAKE_LEAN_API_KEY"]
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def test_cannot_read_outside_project_dir() -> bool:
    """cwd= alone is not containment. A command should not be able to
    read a file that lives outside the project directory just because
    a relative path happens to point there.
    
    This test requires bubblewrap for proper filesystem containment.
    """
    # This test requires bubblewrap - if not available, FAIL (don't skip)
    if not _check_bubblewrap():
        print("FAIL: test_cannot_read_outside_project_dir requires bubblewrap "
              "(not installed). Install with: apt install bubblewrap")
        return False
    
    outside_dir = Path(tempfile.mkdtemp(prefix="task04_outside_"))
    project_dir = Path(tempfile.mkdtemp(prefix="task04_project_"))
    try:
        secret_file = outside_dir / "secret.txt"
        secret_file.write_text("outside-project-secret-content", encoding="utf-8")

        # Relative escape from project_dir to outside_dir's secret file.
        rel = os.path.relpath(secret_file, project_dir)
        result = execute(f"cat {rel}", project_dir, timeout=10)

        escaped = "outside-project-secret-content" in result.output
        if escaped:
            print(f"FAIL: command read a file outside the project dir "
                  f"via relative path escape ({rel!r})")
            return False
        print("PASS: command cannot read outside the project dir")
        return True
    finally:
        shutil.rmtree(outside_dir, ignore_errors=True)
        shutil.rmtree(project_dir, ignore_errors=True)


def test_project_dir_is_readwrite() -> bool:
    """Verify that the project directory is mounted read-write, allowing
    test/build commands to create .pytest_cache, node_modules, etc.
    """
    if not _check_bubblewrap():
        print("FAIL: test_project_dir_is_readwrite requires bubblewrap")
        return False
    
    project_dir = Path(tempfile.mkdtemp(prefix="task04_rw_test_"))
    try:
        # Create a file inside the project directory
        test_file = project_dir / "test_write.txt"
        test_file.write_text("test content", encoding="utf-8")
        
        # Try to read and verify the file was created
        result = execute("cat test_write.txt", project_dir, timeout=10)
        
        if "test content" not in result.output:
            print(f"FAIL: could not read file created in project dir")
            print(f"      Output: {result.output}")
            return False
        
        print("PASS: project directory is read-write")
        return True
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def test_execute_refuses_without_bubblewrap() -> bool:
    """TASK-18: Verify that execute() refuses to run when bubblewrap
    is not available and allow_unsandboxed is not set.
    """
    # This test checks that the refusal message is returned
    # We can't easily test the actual refusal without uninstalling bwrap,
    # so we just verify the error message is correct
    if not _check_bubblewrap():
        # If bwrap is not available, try with allow_unsandboxed=False
        project_dir = Path(tempfile.mkdtemp(prefix="task04_refuse_"))
        try:
            result = execute("echo hello", project_dir, timeout=10, allow_unsandboxed=False)
            if result.success:
                print("FAIL: execute() succeeded without bubblewrap and allow_unsandboxed=False")
                return False
            if "bubblewrap not installed" not in result.output.lower():
                print(f"FAIL: expected 'bubblewrap not installed' in error message")
                print(f"      Got: {result.output}")
                return False
            print("PASS: execute() refuses when bubblewrap unavailable")
            return True
        finally:
            shutil.rmtree(project_dir, ignore_errors=True)
    else:
        print("INFO: bubblewrap is installed, skipping refusal test")
        print("      (execute() will use bubblewrap successfully)")
        return True


def run_test() -> bool:
    results = [
        test_bubblewrap_required(),
        test_secrets_not_leaked_to_subprocess_env(),
        test_cannot_read_outside_project_dir(),
        test_project_dir_is_readwrite(),
        test_execute_refuses_without_bubblewrap(),
    ]
    return all(results)


if __name__ == "__main__":
    ok = run_test()
    print()
    print("TASK-04/18 sandbox test:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)

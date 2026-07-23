"""Verification pipeline - runs tests deterministically.

No AI involved - pure command execution.

Structured output format:
- passed: bool
- failed_tests: list of {name: str, message: str}
- first_failure_summary: str (concise summary of the first failure for quick display)
"""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FailedTest:
    """A single failed test."""
    name: str
    message: str


@dataclass
class VerificationResult:
    """Result of verification run.
    
    Structured format for pytest - only includes test results.
    No lint/syntax fields per SPEC.md Non-Goals.
    """
    passed: bool
    output: str  # Human-readable output
    failed_tests: list[FailedTest] = field(default_factory=list)
    first_failure_summary: str = ""


def detect_project_type(project_path: Path) -> str | None:
    """Detect project type."""
    if (project_path / "pyproject.toml").exists():
        return "python"
    if (project_path / "package.json").exists():
        return "javascript"
    return None


def verify(project_path: Path, timeout: int = 60) -> VerificationResult:
    """Run verification checks on project."""
    project_type = detect_project_type(project_path)
    
    if project_type == "python":
        return _verify_python(project_path, timeout)
    elif project_type == "javascript":
        return _verify_javascript(project_path, timeout)
    
    return VerificationResult(passed=True, output="No verification configured")


def _verify_python(project_path: Path, timeout: int) -> VerificationResult:
    """Verify Python project - pytest only, no lint/syntax per Non-Goals."""
    # Check for tests
    has_tests = any((project_path / d).exists() for d in ["tests", "test", "_tests"])
    
    if has_tests:
        return _run_pytest(project_path, timeout)
    
    return VerificationResult(passed=True, output="No tests found")


def _run_pytest(project_path: Path, timeout: int) -> VerificationResult:
    """Run pytest and parse output into structured format."""
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "-v", "--tb=short"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        
        # Parse pytest output into structured format
        failed_tests, first_summary = _parse_pytest_output(output)
        
        passed = result.returncode == 0 and len(failed_tests) == 0
        
        # Generate human-readable output
        if passed:
            output_parts = ["All tests passed"]
        else:
            output_parts = [f"{len(failed_tests)} test(s) failed:"]
            for test in failed_tests[:5]:  # Limit to first 5
                output_parts.append(f"  - {test.name}: {test.message[:100]}")
            if len(failed_tests) > 5:
                output_parts.append(f"  ... and {len(failed_tests) - 5} more")
        
        return VerificationResult(
            passed=passed,
            output="\n".join(output_parts),
            failed_tests=failed_tests,
            first_failure_summary=first_summary,
        )
        
    except subprocess.TimeoutExpired:
        return VerificationResult(
            passed=False,
            output="TIMEOUT: pytest exceeded time limit",
            failed_tests=[FailedTest(name="pytest", message="timeout")],
            first_failure_summary="Test execution timed out",
        )
    except FileNotFoundError:
        return VerificationResult(passed=True, output="(pytest not found)")
    except Exception as e:
        return VerificationResult(
            passed=False,
            output=f"Error: {str(e)[:200]}",
            failed_tests=[FailedTest(name="pytest", message=str(e)[:200])],
            first_failure_summary=f"Test runner error: {str(e)[:100]}",
        )


def _parse_pytest_output(output: str) -> tuple[list[FailedTest], str]:
    """Parse pytest verbose output into structured failed_tests list.
    
    Pytest output formats:
    1. Inline format (pytest --tb=short):
       FAILED path/test.py::test_name - AssertionError: message
       OR
       path/test.py::test_name FAILED - AssertionError: message
    
    2. Traceback format (pytest --tb=long):
       path/test.py::test_name FAILED
       ____ test_name ____
       ...traceback...
       E   AssertionError: message
    
    Returns (failed_tests, first_failure_summary)
    """
    failed_tests: list[FailedTest] = []
    first_summary = ""
    
    lines = output.split("\n")
    
    # First pass: find all FAILED lines to get test names and inline messages
    failed_test_data: list[tuple[str, str, str]] = []  # (full_name, test_name, inline_message)
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        
        # Match format: path/test.py::test_name FAILED - ErrorType: message (inline)
        inline_match = re.match(r"^(.+)::(\w+)\s+FAILED\s*-\s*(.+)$", stripped)
        if inline_match:
            path = inline_match.group(1)
            test_name = inline_match.group(2)
            inline_msg = inline_match.group(3).strip()
            full_name = f"{path}::{test_name}"
            failed_test_data.append((full_name, test_name, inline_msg))
            continue
        
        # Match format: FAILED path/test.py::test_name - ErrorType: message (inline)
        failed_inline_match = re.match(r"^FAILED\s+([^:]+)::(\w+)\s*-\s*(.+)$", stripped)
        if failed_inline_match:
            path = failed_inline_match.group(1)
            test_name = failed_inline_match.group(2)
            inline_msg = failed_inline_match.group(3).strip()
            full_name = f"{path}::{test_name}"
            failed_test_data.append((full_name, test_name, inline_msg))
            continue
        
        # Match format: path/test.py::test_name FAILED (no message, may have trailing content like [100%])
        simple_match = re.match(r"^(.+)::(\w+)\s+FAILED", stripped)
        if simple_match:
            path = simple_match.group(1)
            test_name = simple_match.group(2)
            full_name = f"{path}::{test_name}"
            failed_test_data.append((full_name, test_name, ""))  # No inline message
            continue
        
        # Match format: FAILED path/test.py::test_name (no message)
        failed_simple_match = re.match(r"^FAILED\s+([^:]+)::(\w+)\s*$", stripped)
        if failed_simple_match:
            path = failed_simple_match.group(1)
            test_name = failed_simple_match.group(2)
            full_name = f"{path}::{test_name}"
            failed_test_data.append((full_name, test_name, ""))
    
    # Deduplicate failed tests by full_name
    seen: dict[str, tuple[str, str]] = {}  # full_name -> (test_name, message)
    for full_name, test_name, inline_message in failed_test_data:
        if full_name not in seen:
            seen[full_name] = (test_name, inline_message)
    
    # Second pass: extract error messages for each failed test from traceback
    for full_name, (test_name, inline_message) in seen.items():
        message = inline_message
        
        # If no inline message, look in traceback section
        if not message:
            traceback_started = False
            traceback_line_count = 0
            for i, line in enumerate(lines):
                # Match traceback header: ____ test_name ____
                if re.match(r"_{5,}\s+" + re.escape(test_name) + r"\s+_{5,}", line):
                    traceback_started = True
                    traceback_line_count = 0
                    continue
                if traceback_started:
                    traceback_line_count += 1
                    # Look for error message patterns - the E lines contain the actual error
                    if line.startswith("E "):
                        # Format: E   AssertionError: message
                        error_match = re.search(r"(AssertionError|ValueError|TypeError|KeyError|IndexError|RuntimeError|Error):\s*(.+)$", line)
                        if error_match:
                            error_type = error_match.group(1)
                            error_msg = error_match.group(2).strip()
                            message = f"{error_type}: {error_msg}"
                            break
                    # Stop after reasonable traceback lines (10 lines max)
                    if traceback_line_count > 10:
                        break
                    # Stop at next test section or summary line
                    if line and not line[0].isspace() and not line[0] in '>EF':
                        if (line.startswith("=") or line.startswith("_")) and len(line) > 5:
                            break
        
        if not message:
            message = "test failed"
        
        failed_tests.append(FailedTest(name=full_name, message=message))
        
        if not first_summary:
            first_summary = f"{test_name}: {message[:150]}"
    
    return failed_tests, first_summary


def _verify_javascript(project_path: Path, timeout: int) -> VerificationResult:
    """Verify JavaScript project - npm test only."""
    import json
    pkg = project_path / "package.json"
    
    if not pkg.exists():
        return VerificationResult(passed=True, output="No package.json")
    
    try:
        data = json.loads(pkg.read_text())
        has_test = "scripts" in data and "test" in data["scripts"]
    except Exception:
        return VerificationResult(
            passed=False,
            output="Invalid package.json",
            failed_tests=[FailedTest(name="package.json", message="parse error")],
            first_failure_summary="package.json is invalid JSON",
        )
    
    if not has_test:
        return VerificationResult(passed=True, output="No test script")
    
    return _run_npm_test(project_path, timeout)


def _run_npm_test(project_path: Path, timeout: int) -> VerificationResult:
    """Run npm test."""
    try:
        result = subprocess.run(
            ["npm", "test"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout + result.stderr)[:500]
        passed = result.returncode == 0
        
        if passed:
            return VerificationResult(passed=True, output="npm test passed")
        else:
            return VerificationResult(
                passed=False,
                output=f"npm test failed: {output[:200]}",
                failed_tests=[FailedTest(name="npm test", message=output[:200])],
                first_failure_summary=f"npm test failed: {output[:150]}",
            )
    except subprocess.TimeoutExpired:
        return VerificationResult(
            passed=False,
            output="TIMEOUT: npm test exceeded time limit",
            failed_tests=[FailedTest(name="npm test", message="timeout")],
            first_failure_summary="Test execution timed out",
        )
    except FileNotFoundError:
        return VerificationResult(passed=True, output="(npm not found)")
    except Exception as e:
        return VerificationResult(
            passed=False,
            output=f"Error: {str(e)[:200]}",
            failed_tests=[FailedTest(name="npm test", message=str(e)[:200])],
            first_failure_summary=f"Test runner error: {str(e)[:100]}",
        )

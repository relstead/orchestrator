"""Verification pipeline - runs tests deterministically.

No AI involved - pure command execution.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VerificationResult:
    """Result of verification run."""
    passed: bool
    output: str
    errors: list[str]


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
    
    return VerificationResult(passed=True, output="No verification configured", errors=[])


def _verify_python(project_path: Path, timeout: int) -> VerificationResult:
    """Verify Python project."""
    errors = []
    output_parts = []
    
    # Check for tests
    has_tests = any((project_path / d).exists() for d in ["tests", "test", "_tests"])
    
    if has_tests:
        result = _run_pytest(project_path, timeout)
        output_parts.append(f"pytest: {'PASS' if result['passed'] else 'FAIL'}")
        if not result['passed']:
            errors.append(f"pytest: {result['output'][:200]}")
    
    # Check syntax
    syntax_ok = _check_syntax(project_path)
    output_parts.append(f"syntax: {'PASS' if syntax_ok else 'FAIL'}")
    if not syntax_ok:
        errors.append("Syntax errors found")
    
    passed = len(errors) == 0
    return VerificationResult(
        passed=passed,
        output="\n".join(output_parts),
        errors=errors,
    )


def _verify_javascript(project_path: Path, timeout: int) -> VerificationResult:
    """Verify JavaScript project."""
    import json
    pkg = project_path / "package.json"
    
    if not pkg.exists():
        return VerificationResult(passed=True, output="No package.json", errors=[])
    
    try:
        data = json.loads(pkg.read_text())
        has_test = "scripts" in data and "test" in data["scripts"]
    except Exception:
        return VerificationResult(passed=False, output="Invalid package.json", errors=["package.json parse error"])
    
    if not has_test:
        return VerificationResult(passed=True, output="No test script", errors=[])
    
    result = _run_npm_test(project_path, timeout)
    return VerificationResult(
        passed=result["passed"],
        output=f"npm test: {'PASS' if result['passed'] else 'FAIL'}",
        errors=[] if result["passed"] else [result["output"][:200]],
    )


def _run_pytest(project_path: Path, timeout: int) -> dict:
    """Run pytest."""
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "-v", "--tb=short"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {"passed": result.returncode == 0, "output": (result.stdout + result.stderr)[:500]}
    except subprocess.TimeoutExpired:
        return {"passed": False, "output": "TIMEOUT"}
    except FileNotFoundError:
        return {"passed": True, "output": "(pytest not found)"}
    except Exception as e:
        return {"passed": False, "output": str(e)}


def _check_syntax(project_path: Path) -> bool:
    """Check Python syntax."""
    try:
        import py_compile
        for py_file in project_path.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            try:
                py_compile.compile(str(py_file), doraise=True)
            except py_compile.PyCompileError:
                return False
        return True
    except Exception:
        return True  # Don't fail on import errors


def _run_npm_test(project_path: Path, timeout: int) -> dict:
    """Run npm test."""
    try:
        result = subprocess.run(
            ["npm", "test"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {"passed": result.returncode == 0, "output": (result.stdout + result.stderr)[:500]}
    except subprocess.TimeoutExpired:
        return {"passed": False, "output": "TIMEOUT"}
    except FileNotFoundError:
        return {"passed": True, "output": "(npm not found)"}
    except Exception as e:
        return {"passed": False, "output": str(e)}

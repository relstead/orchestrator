"""
Verification module for Vault Orchestrator.

Handles task verification and code testing.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .vault import Vault


@dataclass
class VerificationResult:
    """Result of a verification run."""
    passed: bool
    output: str
    error: str | None = None
    return_code: int = 0


class VerificationRunner:
    """
    Runs verification checks on tasks.
    
    Per §14, this doesn't assume a full installed toolchain -
    it runs what's available and reports what it can.
    """
    
    def __init__(self, vault: "Vault"):
        self._vault = vault
    
    def verify_project(self, project_path: Path) -> VerificationResult:
        """
        Verify a project by running available checks.
        
        Checks for:
        - Python syntax (if python files exist)
        - Basic file existence
        """
        results = []
        errors = []
        
        # Check Python files
        python_files = list(project_path.rglob("*.py"))
        if python_files:
            for py_file in python_files:
                result = self._check_python_syntax(py_file)
                results.append(result)
                if not result.passed:
                    errors.append(f"{py_file}: {result.error}")
        
        # Check for required project files
        required_files = ["NOTES.md", "STATUS.md"]
        for fname in required_files:
            fpath = project_path / fname
            if not fpath.exists():
                errors.append(f"Missing required file: {fname}")
        
        all_passed = all(r.passed for r in results) and len(errors) == 0
        
        return VerificationResult(
            passed=all_passed,
            output="\n".join(f"  {r.passed and '✓' or '✗'} {r.output}" for r in results),
            error="\n".join(errors) if errors else None,
        )
    
    def _check_python_syntax(self, py_file: Path) -> VerificationResult:
        """Check Python file for syntax errors."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(py_file)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            
            if result.returncode == 0:
                return VerificationResult(
                    passed=True,
                    output=f"{py_file.name}: OK",
                )
            else:
                return VerificationResult(
                    passed=False,
                    output=f"{py_file.name}: Syntax error",
                    error=result.stderr,
                    return_code=result.returncode,
                )
        except Exception as e:
            return VerificationResult(
                passed=False,
                output=f"{py_file.name}: Error",
                error=str(e),
            )
    
    def verify_task(self, task_file: Path) -> VerificationResult:
        """
        Verify a task file is well-formed.
        
        Checks:
        - Valid meta comment
        - Has title
        - Attempts are valid
        """
        from .tasks import TaskMeta
        
        try:
            content = task_file.read_text()
            
            # Check meta
            meta = TaskMeta.parse(content)
            
            # Check title
            has_title = any(line.startswith("# ") for line in content.splitlines())
            
            if meta.schema_version != 1:
                return VerificationResult(
                    passed=False,
                    output="Task file verification",
                    error=f"Unknown schema version: {meta.schema_version}",
                )
            
            if not has_title:
                return VerificationResult(
                    passed=False,
                    output="Task file verification",
                    error="Missing title (no # heading)",
                )
            
            return VerificationResult(
                passed=True,
                output="Task file verification: OK",
            )
            
        except Exception as e:
            return VerificationResult(
                passed=False,
                output="Task file verification",
                error=str(e),
            )

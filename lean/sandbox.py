"""Sandboxed command execution.

Executes commands in project folder with timeout and safety guards.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


DANGEROUS_PATTERNS = [
    r"\brm\s+-rf?\s+/",
    r"\bmkfs\b",
    r"\bdd\s+.*of=/dev",
    r"\bshutdown\b",
    r"\breboot\b",
    r"curl\b.*\|\s*ba?sh",
    r"wget\b.*\|\s*ba?sh",
]
DANGEROUS_RE = re.compile("|".join(DANGEROUS_PATTERNS), re.IGNORECASE)


@dataclass
class ExecutionResult:
    """Result of command execution."""
    success: bool
    exit_code: int
    output: str
    timed_out: bool


def is_dangerous(command: str) -> bool:
    """Check if command matches dangerous patterns."""
    return DANGEROUS_RE.search(command) is not None


def execute(
    command: str,
    cwd: Path,
    timeout: int = 30,
    max_output: int = 3000,
) -> ExecutionResult:
    """Execute command in sandboxed environment."""
    if not command.strip():
        return ExecutionResult(success=False, exit_code=-1, output="ERROR: empty command", timed_out=False)
    
    if is_dangerous(command):
        return ExecutionResult(success=False, exit_code=-1, output="REFUSED: dangerous command", timed_out=False)
    
    try:
        result = subprocess.run(
            command, shell=True, cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout,
        )
        
        output = (result.stdout or "") + (("\n[stderr]\n" + result.stderr) if result.stderr else "")
        output = output.strip() or "(no output)"
        
        if len(output) > max_output:
            output = output[:max_output] + f"\n...[{len(output) - max_output} more chars]"
        
        return ExecutionResult(
            success=result.returncode == 0,
            exit_code=result.returncode,
            output=output,
            timed_out=False,
        )
    
    except subprocess.TimeoutExpired:
        return ExecutionResult(success=False, exit_code=-1, output=f"TIMEOUT: exceeded {timeout}s", timed_out=True)
    
    except Exception as e:
        return ExecutionResult(success=False, exit_code=-1, output=f"ERROR: {e}", timed_out=False)

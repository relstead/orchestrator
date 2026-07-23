"""Sandboxed command execution.

Executes commands with real isolation using bubblewrap for filesystem and network isolation.
Environment variables are allowlisted to prevent API key leakage.

Filesystem view is limited to the project directory only (read-write) plus system directories (read-only).
Network access is disabled by default.
"""

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


# Dangerous command patterns (final safety net, not primary defense)
DANGEROUS_PATTERNS = [
    r"\brm\s+-rf?\s+/",
    r"\bmkfs\b",
    r"\bdd\s+.*of=/dev",
    r"\bshutdown\b",
    r"\breboot\b",
    r"curl\b.*\|\s*ba?sh",
    r"wget\b.*[|\s]+\s*(ba?sh|sh|python|perl|ruby)",
]
DANGEROUS_RE = re.compile("|".join(DANGEROUS_PATTERNS), re.IGNORECASE)


# Cache for bubblewrap availability check (per process)
_BWRAP_AVAILABLE: bool | None = None


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


def _check_bubblewrap() -> bool:
    """Check if bubblewrap is available and can run (cached per process).
    
    Note: bubblewrap requires user namespace capabilities which may not be
    available in all container environments.
    """
    global _BWRAP_AVAILABLE
    if _BWRAP_AVAILABLE is not None:
        return _BWRAP_AVAILABLE
    
    bubblewrap_path = subprocess.run(
        ["which", "bwrap"], capture_output=True, text=True
    ).stdout.strip()
    
    if not (bubblewrap_path and os.path.exists(bubblewrap_path)):
        _BWRAP_AVAILABLE = False
        return False
    
    # Quick sanity check: try running bwrap with a trivial command
    # If user namespaces aren't allowed, this will fail
    try:
        result = subprocess.run(
            ["bwrap", "--bind", "/tmp", "/tmp", "true"],
            capture_output=True,
            timeout=5,
        )
        _BWRAP_AVAILABLE = (result.returncode == 0)
        return _BWRAP_AVAILABLE
    except Exception:
        _BWRAP_AVAILABLE = False
        return False


def execute(
    command: str,
    cwd: Path,
    timeout: int = 30,
    max_output: int = 3000,
    allow_network: bool = False,
    allow_unsandboxed: bool = False,
) -> ExecutionResult:
    """
    Execute command in sandboxed environment.
    
    Security features:
    - Filesystem view limited to project directory (read-write) plus system dirs (read-only)
    - Network disabled by default (allow_network=True to enable)
    - Environment variables cleared except safe allowlist
    - Process/PID/IPC/UTS namespaces isolated
    
    Args:
        command: The shell command to execute
        cwd: Working directory (project path)
        timeout: Max execution time in seconds
        max_output: Max output characters to return
        allow_network: Whether to allow network access
        allow_unsandboxed: If True, allow execution without bubblewrap (not recommended)
    
    Returns:
        ExecutionResult with success, exit_code, output, and timed_out fields
    """
    if not command.strip():
        return ExecutionResult(success=False, exit_code=-1, output="ERROR: empty command", timed_out=False)
    
    if is_dangerous(command):
        return ExecutionResult(success=False, exit_code=-1, output="REFUSED: dangerous command", timed_out=False)
    
    project_path = cwd.resolve()
    
    # Safe environment - never passes secrets to subprocess
    safe_env = {
        "HOME": str(project_path),
        "TMPDIR": "/tmp",
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    
    try:
        # Bubblewrap is required for proper filesystem containment
        if _check_bubblewrap():
            return _execute_with_bubblewrap(
                command, project_path, timeout, max_output, allow_network, safe_env
            )
        
        # No sandbox available - refuse to run unless explicitly opted in
        if allow_unsandboxed:
            return _execute_basic_isolation(
                command, project_path, timeout, max_output, safe_env
            )
        
        return ExecutionResult(
            success=False,
            exit_code=-1,
            output="REFUSED: bubblewrap not installed, cannot provide filesystem isolation. "
                   "Install bubblewrap: apt install bubblewrap. "
                   "Or set allow_unsandboxed=True to run without isolation.",
            timed_out=False,
        )
    
    except subprocess.TimeoutExpired:
        return ExecutionResult(success=False, exit_code=-1, output=f"TIMEOUT: exceeded {timeout}s", timed_out=True)
    
    except Exception as e:
        return ExecutionResult(success=False, exit_code=-1, output=f"ERROR: {e}", timed_out=False)


def _execute_with_bubblewrap(
    command: str,
    project_path: Path,
    timeout: int,
    max_output: int,
    allow_network: bool,
    safe_env: dict[str, str],
) -> ExecutionResult:
    """Execute using bubblewrap for proper isolation."""
    project_str = str(project_path.resolve())
    
    # Build bubblewrap command for filesystem containment
    bwrap_cmd = [
        "bwrap",
        "--unshare-user",          # New user namespace
        "--unshare-pid",          # New PID namespace  
        "--unshare-uts",          # New UTS namespace (hostname)
        "--unshare-ipc",          # New IPC namespace
        "--die-with-parent",      # Kill child if parent dies
        "--clearenv",             # Clear all env vars (correct flag name)
    ]
    
    if not allow_network:
        bwrap_cmd.append("--unshare-net")  # No network access
    
    # Filesystem isolation: create a minimal view
    # Bind system directories read-only so shell/interpreter can execute
    # Project directory is read-write so tests/builds can write (.pytest_cache, node_modules, etc.)
    bwrap_cmd.extend([
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--tmpfs", "/tmp",
        "--dev-bind", "/dev/null", "/dev/null",
        "--proc", "/proc",
        "--bind", project_str, project_str,  # Read-write for project dir
    ])
    
    # Add only safe environment variables
    for key, value in safe_env.items():
        bwrap_cmd.extend(["--setenv", key, value])
    
    # The command to run
    bwrap_cmd.extend(["sh", "-c", f"cd '{project_str}' && {command}"])
    
    result = subprocess.run(
        bwrap_cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
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


def _execute_basic_isolation(
    command: str,
    project_path: Path,
    timeout: int,
    max_output: int,
    safe_env: dict[str, str],
) -> ExecutionResult:
    """
    Basic isolation without bubblewrap (opt-in only).
    
    WARNING: This provides NO real filesystem containment. Only use this if:
    1. You explicitly opt-in with allow_unsandboxed=True
    2. You understand that executed commands can read/write ANY file on the system
    
    This function exists only to support environments where bubblewrap cannot be installed.
    It prevents API key leakage by clearing environment variables, but does not
    restrict filesystem access at all.
    """
    # Check if we can use unshare for basic namespace isolation
    unshare_available = subprocess.run(
        ["which", "unshare"], capture_output=True, text=True
    ).stdout.strip() and os.path.exists(
        subprocess.run(["which", "unshare"], capture_output=True, text=True).stdout.strip()
    )
    
    if unshare_available:
        unshare_path = subprocess.run(
            ["which", "unshare"], capture_output=True, text=True
        ).stdout.strip()
        
        # Try to create a private mount namespace with a restricted view
        # This requires CAP_SYS_ADMIN (usually available in containers)
        try:
            # Create a temporary directory that will become our new root
            with tempfile.TemporaryDirectory() as tmpdir:
                # Create a minimal root structure
                rootfs = Path(tmpdir) / "rootfs"
                rootfs.mkdir()
                (rootfs / "project").mkdir()
                
                # Try to use pivot_root for proper containment
                # This is complex and may fail without proper privileges
                script = f"""
                    mount --bind {project_path} {rootfs / "project"} 2>/dev/null || true
                    cd {rootfs / "project"}
                    {command}
                """
                
                # Try unshare with mount namespace
                result = subprocess.run(
                    [unshare_path, "--mount", "--pid", "--fork"],
                    input=script,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=safe_env,
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
        except Exception:
            pass  # Fall through to basic execution
    
    # Basic execution with cleared environment (no true filesystem containment)
    result = subprocess.run(
        command,
        shell=True,
        cwd=str(project_path),
        env=safe_env,
        capture_output=True,
        text=True,
        timeout=timeout,
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

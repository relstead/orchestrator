"""Structured logging with JSON output support."""

import json
import os
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class LogLevel(Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Logger:
    """
    Structured logger that outputs JSON to file and optionally to stdout.
    
    JSON format enables integration with dashboards, log aggregators, etc.
    """
    
    def __init__(
        self,
        log_file: Path | None = None,
        level: LogLevel = LogLevel.INFO,
        json_output: bool = True,
        include_console: bool = True,
    ):
        self.log_file = log_file
        self.level = level
        self.json_output = json_output
        self.include_console = include_console
        self._file_handle = None
        
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            self._file_handle = open(log_file, "a", encoding="utf-8")
    
    def close(self) -> None:
        """Close the log file."""
        if self._file_handle:
            self._file_handle.close()
            self._file_handle = None
    
    def __del__(self):
        self.close()
    
    def _should_log(self, level: LogLevel) -> bool:
        """Check if this level should be logged."""
        levels = list(LogLevel)
        return levels.index(level) >= levels.index(self.level)
    
    def _format_json(self, level: str, message: str, **extra: Any) -> str:
        """Format log entry as JSON."""
        entry = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "level": level,
            "message": message,
        }
        # Add extra fields (flattened for clarity)
        for key, value in extra.items():
            if value is not None:
                entry[key] = value
        return json.dumps(entry, ensure_ascii=False)
    
    def _format_human(self, level: str, message: str, **extra: Any) -> str:
        """Format log entry as human-readable."""
        ts = datetime.now().strftime("%H:%M:%S")
        extra_str = " ".join(f"{k}={v}" for k, v in extra.items() if v is not None)
        if extra_str:
            return f"[{ts}] [{level.upper():8}] {message} ({extra_str})"
        return f"[{ts}] [{level.upper():8}] {message}"
    
    def _write(self, level: str, message: str, **extra: Any) -> None:
        """Write a log entry."""
        if not self._should_log(LogLevel(level)):
            return
        
        if self.json_output:
            line = self._format_json(level, message, **extra)
        else:
            line = self._format_human(level, message, **extra)
        
        if self._file_handle:
            self._file_handle.write(line + "\n")
            self._file_handle.flush()
        
        if self.include_console:
            print(line, file=sys.stderr)
    
    def debug(self, message: str, **extra: Any) -> None:
        """Log debug message."""
        self._write("debug", message, **extra)
    
    def info(self, message: str, **extra: Any) -> None:
        """Log info message."""
        self._write("info", message, **extra)
    
    def warning(self, message: str, **extra: Any) -> None:
        """Log warning message."""
        self._write("warning", message, **extra)
    
    def error(self, message: str, **extra: Any) -> None:
        """Log error message."""
        self._write("error", message, **extra)
    
    # Aliases matching common conventions
    warn = warning
    err = error


def get_default_logger(vault_root: Path | None = None) -> Logger:
    """Create a default logger for the orchestrator."""
    log_dir = None
    if vault_root:
        log_dir = vault_root / "_logs"
    
    # Check env var for log level
    level = LogLevel.INFO
    if os.environ.get("LEAN_LOG_LEVEL", "").upper() in ("DEBUG",):
        level = LogLevel.DEBUG
    elif os.environ.get("LEAN_LOG_LEVEL", "").upper() in ("WARNING", "ERROR"):
        level = LogLevel.WARNING
    
    # Check env var for JSON output
    json_output = os.environ.get("LEAN_JSON_LOG", "true").lower() != "false"
    
    log_file = None
    if log_dir:
        log_file = log_dir / "orchestrator.jsonl"
    
    return Logger(
        log_file=log_file,
        level=level,
        json_output=json_output,
        include_console=True,
    )

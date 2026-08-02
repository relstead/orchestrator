"""
Vault Orchestrator - Windows-native sandboxed AI task runner.

A lightweight desktop app that lets free-tier / open API models work
semi-autonomously across multiple projects against an Obsidian vault.
"""

from lean.config import Config
from lean.vault import Vault, VaultError
from lean.tasks import Task, TaskMeta, TaskState, TaskStore, TaskType
from lean.dependency import DependencyGraph, build_graph
from lean.indexer import Indexer, ProjectIndex
from lean.agent import Action, ActionType, parse_action
from lean.sandbox import (
    AppContainerSandbox,
    JobObject,
    RestrictedTokenSandbox,
    SpawnResult,
    is_command_allowed,
    safe_vault_path,
    ACLSetup,
)
from lean.logger import MetricsWriter, DigestWriter, TaskEvent
from lean.worker import Worker, WorkerPool
from lean.planner import PlannerValidator
from lean.verification import VerificationRunner, VerificationResult
from lean.orchestrator import Orchestrator, OrchestratorState
from lean.cli import main as cli_main

__all__ = [
    "Config",
    "Vault",
    "VaultError",
    "Task",
    "TaskMeta",
    "TaskState",
    "TaskStore",
    "TaskType",
    "DependencyGraph",
    "build_graph",
    "Indexer",
    "ProjectIndex",
    "Action",
    "ActionType",
    "parse_action",
    "AppContainerSandbox",
    "JobObject",
    "RestrictedTokenSandbox",
    "SpawnResult",
    "is_command_allowed",
    "safe_vault_path",
    "ACLSetup",
    "MetricsWriter",
    "DigestWriter",
    "TaskEvent",
    "Worker",
    "WorkerPool",
    "PlannerValidator",
    "VerificationRunner",
    "VerificationResult",
    "Orchestrator",
    "OrchestratorState",
    "cli_main",
]

__version__ = "0.1.0"

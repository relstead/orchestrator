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
from lean.worker import Worker, WorkerPool, TaskContext, TaskExecutionResult
from lean.planner import PlannerValidator
from lean.verification import VerificationRunner, VerificationResult
from lean.orchestrator import Orchestrator, OrchestratorState
from lean.provider import (
    ProviderClient,
    GroqProvider,
    OpenRouterProvider,
    Message,
    CompletionResult,
    create_provider,
)
from lean.agent_loop import AgentLoop, TaskTranscript, TaskResult, Turn, build_system_prompt
from lean.dispatcher import AgentDispatcher, ActionResult, parse_action as parse_dispatcher_action
from lean.cli import main as cli_main

__all__ = [
    # Core
    "Config",
    "Vault",
    "VaultError",
    # Tasks
    "Task",
    "TaskMeta",
    "TaskState",
    "TaskStore",
    "TaskType",
    # Dependency
    "DependencyGraph",
    "build_graph",
    # Indexer
    "Indexer",
    "ProjectIndex",
    # Agent
    "Action",
    "ActionType",
    "parse_action",
    # Sandbox
    "AppContainerSandbox",
    "JobObject",
    "RestrictedTokenSandbox",
    "SpawnResult",
    "is_command_allowed",
    "safe_vault_path",
    "ACLSetup",
    # Logger
    "MetricsWriter",
    "DigestWriter",
    "TaskEvent",
    # Worker
    "Worker",
    "WorkerPool",
    "TaskContext",
    "TaskExecutionResult",
    # Planner
    "PlannerValidator",
    # Verification
    "VerificationRunner",
    "VerificationResult",
    # Orchestrator
    "Orchestrator",
    "OrchestratorState",
    # Provider
    "ProviderClient",
    "GroqProvider",
    "OpenRouterProvider",
    "Message",
    "CompletionResult",
    "create_provider",
    # Agent Loop
    "AgentLoop",
    "TaskTranscript",
    "TaskResult",
    "Turn",
    "build_system_prompt",
    # Dispatcher
    "AgentDispatcher",
    "ActionResult",
    "parse_dispatcher_action",
    # CLI
    "cli_main",
]

__version__ = "1.0.0"

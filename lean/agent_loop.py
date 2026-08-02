"""
Agent Loop - executes tasks via LLM turns.

Per §10.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config
    from .provider import Message, ProviderClient, CompletionResult


@dataclass
class Turn:
    """A single turn in the agent loop."""
    turn_num: int
    messages: list[Message] = field(default_factory=list)
    response: str | None = None
    actions: list[dict] = field(default_factory=list)
    error: str | None = None


@dataclass
class TaskTranscript:
    """Transcript of a task execution."""
    task_id: str
    task_type: str
    turns: list[Turn] = field(default_factory=list)
    outcome: str | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    
    @property
    def total_turns(self) -> int:
        return len(self.turns)
    
    def add_turn(self, turn: Turn) -> None:
        self.turns.append(turn)
    
    def to_json(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "outcome": self.outcome,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "turns": [
                {
                    "turn_num": t.turn_num,
                    "messages": [{"role": m.role, "content": m.content} for m in t.messages],
                    "response": t.response,
                    "actions": t.actions,
                    "error": t.error,
                }
                for t in self.turns
            ],
        }


@dataclass
class TaskResult:
    """Result of a task execution."""
    outcome: str  # done, timeout, error, failed
    transcript: TaskTranscript
    actions_taken: list[dict]
    error: str | None = None


class AgentLoop:
    """
    Agent loop for executing tasks.
    
    Manages turns, message history, and action execution.
    """
    
    def __init__(
        self,
        provider: ProviderClient,
        config: Config,
        task_id: str,
        task_type: str,
        system_prompt: str,
    ):
        self.provider = provider
        self.config = config
        self.task_id = task_id
        self.task_type = task_type
        self.system_prompt = system_prompt
        
        self.transcript = TaskTranscript(task_id=task_id, task_type=task_type)
        self.max_turns = (
            config.budgets.planning_max_turns if task_type == "plan"
            else config.budgets.coding_max_turns
        )
        self._context: dict[str, Any] = {}
    
    async def run(
        self,
        initial_message: str,
        context: dict[str, Any] | None = None,
    ) -> TaskResult:
        """
        Run the agent loop.
        
        Args:
            initial_message: The task prompt
            context: Additional context (project info, files, etc.)
        
        Returns:
            TaskResult with outcome and transcript
        """
        self._context = context or {}
        
        # Build initial messages
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": initial_message},
        ]
        
        # Run turns
        for turn_num in range(1, self.max_turns + 1):
            turn = Turn(turn_num=turn_num)
            
            try:
                # Convert to provider format
                provider_messages = [
                    Message(role=m["role"], content=m["content"])
                    for m in messages
                ]
                
                # Get completion
                result = await self.provider.complete(
                    messages=provider_messages,
                    model=self._get_model(),
                    max_tokens=self._get_max_tokens(),
                    temperature=0.7,
                )
                
                turn.response = result.content
                turn.messages = provider_messages[1:]  # Exclude system
                
                # Parse actions from response
                actions = self._parse_actions(result.content)
                turn.actions = actions
                
                # Add assistant response to messages
                messages.append({"role": "assistant", "content": result.content})
                
                # Check for completion
                if self._is_done(actions):
                    self.transcript.outcome = "done"
                    self.transcript.ended_at = time.time()
                    return TaskResult(
                        outcome="done",
                        transcript=self.transcript,
                        actions_taken=[a for t in self.transcript.turns for a in t.actions],
                    )
                
                # Check for blocking actions that need human input
                blocking = self._get_blocking_action(actions)
                if blocking:
                    self.transcript.outcome = "blocked"
                    self.transcript.ended_at = time.time()
                    return TaskResult(
                        outcome="blocked",
                        transcript=self.transcript,
                        actions_taken=[a for t in self.transcript.turns for a in t.actions],
                        error=blocking.get("reason", "Human review required"),
                    )
                
                # Continue loop
                self.transcript.add_turn(turn)
                
            except Exception as e:
                turn.error = str(e)
                self.transcript.add_turn(turn)
                self.transcript.outcome = "error"
                self.transcript.ended_at = time.time()
                return TaskResult(
                    outcome="error",
                    transcript=self.transcript,
                    actions_taken=[a for t in self.transcript.turns for a in t.actions],
                    error=str(e),
                )
        
        # Max turns reached
        self.transcript.outcome = "timeout"
        self.transcript.ended_at = time.time()
        return TaskResult(
            outcome="timeout",
            transcript=self.transcript,
            actions_taken=[a for t in self.transcript.turns for a in t.actions],
            error=f"Max turns ({self.max_turns}) reached",
        )
    
    def _get_model(self) -> str:
        """Get model for task type."""
        if self.task_type == "plan":
            return self.config.workers[0].model if self.config.workers else "llama-3.3-70b-versatile"
        return self.config.workers[0].model if self.config.workers else "llama-3.3-70b-versatile"
    
    def _get_max_tokens(self) -> int:
        """Get max tokens for task type."""
        if self.task_type == "plan":
            return self.config.budgets.planning_max_output_tokens
        return self.config.budgets.coding_max_output_tokens
    
    def _parse_actions(self, response: str) -> list[dict]:
        """
        Parse actions from LLM response.
        
        Expected format: ```json\n[{...}]\n```
        Falls back to regex extraction if JSON not found.
        """
        import re
        from .agent import parse_action
        
        # Try JSON block first
        json_match = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", response)
        if json_match:
            try:
                actions_data = json.loads(json_match.group(1))
                actions = []
                for action_data in actions_data:
                    action = parse_action(action_data)
                    if action:
                        actions.append(action)
                return actions
            except json.JSONDecodeError:
                pass
        
        # Try inline JSON
        json_match = re.search(r"\[\s*\{[\s\S]*?\}\s*\]", response)
        if json_match:
            try:
                actions_data = json.loads(json_match.group(0))
                actions = []
                for action_data in actions_data:
                    action = parse_action(action_data)
                    if action:
                        actions.append(action)
                return actions
            except json.JSONDecodeError:
                pass
        
        return []
    
    def _is_done(self, actions: list[dict]) -> bool:
        """Check if task is complete."""
        for action in actions:
            if action.get("type") == "done":
                return True
        return False
    
    def _get_blocking_action(self, actions: list[dict]) -> dict | None:
        """Get blocking action if any."""
        for action in actions:
            if action.get("type") == "human_review":
                return action
        return None


def build_system_prompt(task_type: str, config: Config | None = None) -> str:
    """
    Build system prompt for task type.
    
    Per §10.
    """
    if task_type == "plan":
        return """You are a planning agent. Break down tasks into smaller subtasks.

For each subtask:
- Give it a clear title
- Specify the project it belongs to
- Add dependencies on other tasks if needed
- Include detailed instructions

Output your plan as a JSON array of tasks:
```json
[
  {"title": "Task Title", "project": "project-name", "body": "Details...", "depends_on": []}
]
```

Or if the task is simple enough, mark it done:
```json
[{"type": "done", "reason": "Task is complete"}]
```"""
    
    return """You are a coding agent. Execute tasks in the sandbox.

Available actions:
- read: Read file contents
- write: Create or overwrite a file
- execute: Run a command in the sandbox

When done, output:
```json
[{"type": "done", "reason": "Completed successfully"}]
```

Use JSON format for all action outputs."""

"""Resumable step-wise driver for the AgentLoop generator.

The generator owns run state. External callers receive proposed actions and
must return observations before the model can continue, preventing an action
from being mistaken for an executed environment effect.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .privacy import CURRENT, activate


@dataclass(frozen=True)
class AgentStep:
    kind: str
    actions: tuple = field(default_factory=tuple)
    observations: tuple = field(default_factory=tuple)
    message: str = ""
    answer: str = ""
    task_state: Any = None


@dataclass(frozen=True)
class StepInput:
    execute_local: bool = False
    observations: Any = None


class AgentRun:
    def __init__(self, agent, *, execute_actions=True, privacy_guard=None):
        from .agent_loop import AgentLoop

        self.agent = agent
        self.execute_actions = bool(execute_actions)
        # Preserve the guard installed by legacy private_run(ask). Using
        # activate(None) here would silently disable the existing privacy boundary.
        self.privacy_guard = privacy_guard if privacy_guard is not None else CURRENT.get()
        self.loop = AgentLoop(agent)
        self._generator = None
        self.started = False
        self.done = False
        self.closed = False
        self.started_at = None
        self.user_message = None
        self.final_answer = None
        self.error = None
        self._awaiting_observation = False
        self._pending_action_count = 0

    @property
    def current_task_state(self):
        return self.agent.current_task_state

    def start(self, user_message):
        if self.started:
            raise RuntimeError("AgentRun has already started")
        self._generator = self.loop.run_steps(user_message)
        self.user_message = user_message
        self.started_at = time.monotonic()
        self.started = True
        return self

    def inject_event(self, event):
        if self.done:
            raise RuntimeError("Cannot inject an event into a finished run")
        self.loop.pending_events.append(event)
        if self.privacy_guard:
            self.privacy_guard.observe(text=json.dumps(event, ensure_ascii=False, default=str))

    def _advance(self, value=None):
        try:
            if value is None:
                return next(self._generator)
            return self._generator.send(value)
        except StopIteration as stop:
            self.done = True
            self.final_answer = stop.value
            return AgentStep("final", answer="" if stop.value is None else str(stop.value),
                             task_state=self.current_task_state)
        except Exception as exc:
            self.error = exc
            state = self.current_task_state
            if state is not None and getattr(state, 'status', '') == 'running':
                self.close()
            else:
                self.done = True
            raise

    def _validate_observation_shape(self, observation):
        if isinstance(observation, (list, tuple)):
            count = len(observation)
        elif self._pending_action_count == 1:
            count = 1
        else:
            raise ValueError('Multiple actions require a list of observations')
        if count != self._pending_action_count:
            raise ValueError(f'Expected {self._pending_action_count} observations, got {count}')

    def step(self, observation=None):
        if not self.started:
            raise RuntimeError("Call start(user_message) before step()")
        if self.done:
            if self.closed:
                return AgentStep("closed", message="Run closed before completion",
                                 task_state=self.current_task_state)
            return AgentStep("final", answer="" if self.final_answer is None else str(self.final_answer),
                             task_state=self.current_task_state)
        if self._awaiting_observation and observation is None:
            raise ValueError("The previous external action needs an observation before step() can continue")
        if not self._awaiting_observation and observation is not None:
            raise ValueError("Unexpected observation: no external action is awaiting a result")
        with activate(self.privacy_guard):
            if self._awaiting_observation:
                self._validate_observation_shape(observation)
                self._awaiting_observation = False
                self._pending_action_count = 0
                step = self._advance(StepInput(observations=observation))
            else:
                step = self._advance()
            if step.kind == "action" and self.execute_actions:
                step = self._advance(StepInput(execute_local=True))
            elif step.kind == "action":
                self._awaiting_observation = True
                self._pending_action_count = len(step.actions)
            return step

    def run_to_completion(self):
        while not self.done:
            self.step()
        return self.final_answer

    def close(self):
        if self.done:
            return
        with activate(self.privacy_guard):
            conversation = getattr(self.agent.model_client, 'native_conversation', None)
            while conversation is not None and conversation.pending:
                self.agent.model_client.observe_native_tool('Not executed: run closed by caller')
            state = self.current_task_state
            if state is not None and getattr(state, 'status', '') == 'running':
                self.loop._finish_stopped(
                    state, self.user_message, 'cancelled',
                    'Run closed by caller before completion.', self.started_at or time.monotonic(),
                )
            if self._generator is not None:
                self._generator.close()
            self.closed = True
            self.done = True

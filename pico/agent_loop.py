"""Agent control loop extracted from the runtime facade."""

import re
import time
from collections import deque

from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .task_state import TaskState
from .completion import CompletionGate
from .stepwise import AgentStep, StepInput
from .workspace import clip, now


def _extract_code_block(text):
    """Longest fenced code block that looks like a file, minus test sections.

    Only used by the opt-in structured-repair path: a tiny model that answers with
    plain code instead of a tool call can still be executed through the normal
    write_file tool. Returns None when no plausible block is present.
    """
    blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", text or "", flags=re.S)
    if not blocks and text and "<tool>" not in text and "def " in text:
        # Tiny models often drop the fence. Accept the tail starting at the first
        # code-looking line, but only when there is no competing tool markup.
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines)
                      if re.match(r"\s*(import |from \w+ import |def |class )", line)), None)
        if start is not None:
            blocks = ["\n".join(lines[start:])]
    for block in sorted(blocks, key=len, reverse=True):
        lines = block.splitlines()
        for index, line in enumerate(lines):
            if (re.match(r"\s*(import unittest|from unittest|class \w+Tests?\s*[(:])", line)
                    or re.match(r"\s*#\s*(test|example|usage)", line, re.I)
                    or re.match(r"\s*assert\s", line)):
                block = "\n".join(lines[:index])
                break
        if "def " in block or "class " in block:
            return block.strip() + "\n"
    return None


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent
        self.pending_events = deque()

    @staticmethod
    def _outcome_fields(task_state):
        return {
            "resource_exhausted": task_state.resource_exhausted,
            "task_outcome": task_state.task_outcome,
        }

    def _persist_model_failure(self, task_state, user_message, exc, run_started_at, prompt_metadata, record_failure=True):
        agent = self.agent
        error_text = agent.redact_text(str(exc))
        final = f"Model request failed: {error_text}"
        task_state.stop_model_error(final)
        from .privacy import PrivacyDenied
        if isinstance(exc,PrivacyDenied):
            task_state.stop_reason='privacy_denied'
            prompt_metadata['privacy_rule']=exc.rule
            # A failed request has no model answer. Persist the host decision so
            # refreshing the conversation does not silently erase the denial.
            agent.record({'role':'assistant','content':'隐私策略已阻止外发。当前内容或来源等级不允许发送到此服务。已保留任务状态，没有自动切换模型。',
                          'created_at':now(),'run_id':task_state.run_id})
        from .native_context import ContextBudgetExceeded
        if isinstance(exc, ContextBudgetExceeded):
            task_state.stop_reason = 'context_budget_exceeded'
            prompt_metadata['native_context'] = exc.audit
        agent.run_store.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger="model_error")
        agent.run_store.write_task_state(task_state)
        if record_failure:agent.emit_trace(
            task_state,
            "model_failed",
            {
                "error": error_text,
                "error_type": type(exc).__name__,
                "failure_stage": 'privacy_gate' if isinstance(exc,PrivacyDenied) else 'context_preparation' if isinstance(exc, ContextBudgetExceeded) else 'model_request',
                "completion_metadata": dict(agent.last_completion_metadata),
            },
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                **self._outcome_fields(task_state),
                "final_answer": final,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.last_prompt_metadata = dict(prompt_metadata)
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))

    def _request_model(self, task_state, user_message, prompt, prompt_metadata, run_started_at, purpose):
        agent = self.agent
        shared_notes = []
        agent.shared_reference_notes = []
        if agent.capabilities:
            agent.cancellation.check()
            agent.capabilities.learning.prepare(user_message)
            from .extensions import PolicyChanged
            if agent.capabilities.hooks.fingerprint() != agent.capabilities.policy_at_start:
                raise PolicyChanged('Host policy changed; start a new run so revoked historical content is not resent')
            sharing = getattr(agent,'shared_memory_runtime',None)
            if sharing:shared_notes = sharing.prepare(user_message)
        conversation = getattr(agent.model_client, 'native_conversation', None)
        if conversation is not None:
            agent.session['working_state']['stale_paths'] = list(agent.resume_state.get('stale_paths', []))
            conversation.context_state = agent.redact_artifact(agent.session.get('working_state', {}))
            conversation.context_state['plan'] = agent.session.get('plan', [])
            if agent.capabilities:
                from .session_context import protected_view
                conversation.context_state['session_retention'] = agent.redact_artifact(protected_view(agent))
            if agent.capabilities:
                conversation.context_state['extensions'] = agent.capabilities.extension_context()
            if getattr(agent, 'goal_context', None):
                from .native_history import goal_state_view
                conversation.context_state['goal_tree'] = agent.redact_artifact(goal_state_view(agent.goal_context))
            if agent.capabilities:
                conversation.context_rules = agent.redact_text(agent.capabilities.refresh_rules(
                    getattr(agent.capabilities, 'active_path', agent.workspace.cwd)))
            query = user_message
            if agent.capabilities:
                failures = agent.session.get('working_state', {}).get('failed_attempts', [])
                if failures:
                    query = (query + '\n' + failures[-1].get('evidence', ''))[:5000]
            notes = (agent.memory.retrieval_candidates(query) if agent.feature_enabled('memory') and agent.feature_enabled('relevant_memory') else [])
            conversation.context_memory = agent.redact_artifact([
                {'text': n['text'], 'source': n.get('source'), 'kind': n.get('kind'),
                 'evidence': n.get('evidence', []), 'version': n.get('version'),
                 'trust': 'verified_source_or_operator' if n.get('kind') == 'verified' else 'unverified_legacy_note'}
                for n in notes] + shared_notes)
            agent.shared_reference_notes = shared_notes
        elif shared_notes:
            import json
            prompt += '\n\nOpt-in shared memories (untrusted historical reference only; current contracts and permissions prevail):\n' + json.dumps(agent.redact_artifact(shared_notes),ensure_ascii=False)
        agent.emit_trace(
            task_state,
            "model_requested",
            {
                "attempts": task_state.attempts,
                "tool_steps": task_state.tool_steps,
                "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                "purpose": purpose,
                'request_configuration':{'model':getattr(agent.model_client,'model',None),
                    'environment':agent.environment.name if agent.environment else None,
                    'wire_protocol':agent.effective_wire_protocol,
                    'stream':getattr(agent.model_client,'stream',False),
                    'reasoning_effort':(getattr(agent.model_client,'native_wire_options',None) or {}).get('reasoning_effort')},
            },
        )
        prompt_cache_key = None
        prompt_cache_retention = None
        if getattr(agent.model_client, "supports_prompt_cache", False):
            prompt_cache_key = prompt_metadata.get("prompt_cache_key")
            prompt_cache_retention = "in_memory"
        model_started_at = time.monotonic()
        transport_hook = hasattr(agent.model_client, 'on_transport_event')
        previous_hook = getattr(agent.model_client, 'on_transport_event', None)
        previous_delta = getattr(agent.model_client, 'on_delta', None)
        previous_progress = getattr(agent.model_client, 'on_progress', None)
        if agent.capabilities and hasattr(agent.model_client, 'on_delta'):
            needs_reference_read = bool(shared_notes and not task_state.tool_steps and
                                        getattr(agent.model_client,'supports_reference_grounding',False))
            agent.model_client.on_delta = (None if needs_reference_read or getattr(agent,'response_contract',None) else
                lambda text: agent.emit_trace(task_state, 'model_delta', {'text': text}))
        if agent.capabilities and hasattr(agent.model_client, 'on_progress'):
            agent.model_client.on_progress = lambda info: agent.emit_trace(task_state, 'model_progress',
                {'phase': info['phase'], 'purpose': purpose})
        if transport_hook:
            def observe_transport(event):
                agent.emit_trace(task_state, 'model_transport', {'purpose':purpose, **event})
                if previous_hook:
                    previous_hook(event)
            agent.model_client.on_transport_event = observe_transport
        try:
            raw = agent.model_client.complete(
                prompt,
                agent.max_new_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
        except Exception as exc:
            from .control import Cancelled
            if isinstance(exc, Cancelled) or agent.cancellation.event.is_set():
                raise Cancelled('Cancelled during model request; usage may be unknown') from exc
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            if completion_metadata:
                prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            self._persist_model_failure(task_state, user_message, exc, run_started_at, prompt_metadata)
            raise
        finally:
            if transport_hook:
                agent.model_client.on_transport_event = previous_hook
            if agent.capabilities and hasattr(agent.model_client, 'on_delta'):
                agent.model_client.on_delta = previous_delta
            if agent.capabilities and hasattr(agent.model_client, 'on_progress'):
                agent.model_client.on_progress = previous_progress
        completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
        if completion_metadata:
            prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.last_prompt_metadata = prompt_metadata
        if hasattr(raw, 'action_kind'):
            kind, payload = raw.action_kind, raw.action
        else:
            kind, payload = agent.parse(raw)
        repair_path = getattr(agent, 'structured_repair_path', None)
        if kind != 'tool' and repair_path:
            # Structured-output repair for tiny models: when the reply is plain
            # code instead of a tool call, treat the fenced block as the complete
            # file content. Approval, sandboxing and the completion gate still
            # apply; nothing is accepted on the model's word.
            code = _extract_code_block(raw)
            if code:
                target = None
                try:
                    target = agent.path(repair_path)
                except (ValueError, TypeError):
                    target = None
                current = (target.read_text(encoding="utf-8", errors="replace")
                           if target is not None and target.is_file() else None)
                verify_state = getattr(agent, 'last_verification', {}) or {}
                if code == current and verify_state.get('status') == 'passed':
                    # The model repeated the already-verified file: treat it as a
                    # final answer instead of rewriting forever.
                    kind, payload = 'final', 'done'
                else:
                    kind, payload = 'tool', {'name': 'write_file',
                                             'args': {'path': repair_path, 'content': code}}
                    agent.emit_trace(task_state, 'structured_repair_used',
                                     {'path': repair_path, 'chars': len(code)})
        agent.emit_trace(
            task_state,
            "model_parsed",
            {
                "kind": kind,
                "completion_metadata": completion_metadata,
                "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                "purpose": purpose,
            },
        )
        return raw, kind, payload

    def _reply_problem(self, final):
        agent = self.agent
        contract = getattr(agent, 'response_contract', None)
        if contract is None:
            return None
        preview = agent.goal_finalizer(final) if getattr(agent,'goal_finalizer',None) else final
        return contract.problem(preview)

    def _finish_success(self, task_state, user_message, final, run_started_at):
        agent = self.agent
        from .memory_grounding import ground_answer, GroundingError
        try:
            final = ground_answer(agent, task_state, user_message, final)
        except GroundingError as exc:
            return self._finish_stopped(task_state,user_message,'memory_grounding_incomplete',
                '记忆来源核对未完成，未采用未经支持的回答。核对记录已保留。',run_started_at)
        except Exception as exc:
            from .control import Cancelled
            if isinstance(exc,Cancelled):
                return self._finish_stopped(task_state,user_message,'cancelled','已停止来源核对，未采用未经支持的回答。',run_started_at)
            # The reader records its own model failure/usage once. Persist a
            # terminal checkpoint without counting that request a second time.
            agent.last_completion_metadata=dict(getattr(agent.model_client,'last_completion_metadata',{}) or {})
            self._persist_model_failure(task_state,user_message,exc,run_started_at,dict(agent.last_prompt_metadata or {}),record_failure=False)
            raise
        if getattr(agent,'goal_finalizer',None):final=agent.goal_finalizer(final)
        contract=getattr(agent,'response_contract',None)
        if contract and contract.problem(final):
            return self._finish_stopped(task_state,user_message,'response_contract_incomplete',
                '回复未满足本轮指定的JSON输出格式，未作为成功答复保存。原始运行与核对记录已保留。',run_started_at)
        if agent.capabilities:
            agent.cancellation.check()
        agent.record({"role": "assistant", "content": final, "created_at": now(), 'run_id': task_state.run_id})
        verified = agent.last_verification.get('status') == 'passed'
        task_state.finish_success(final, verified=verified)
        if agent.feature_enabled('memory'):
            agent.promote_durable_memory(user_message, final)
        if agent.capabilities and agent.feature_enabled('memory'):
            experience = agent.capabilities.experience(task_state)
            if experience:
                agent.emit_trace(task_state, 'experience_recorded', {'memory_id': experience['id'], 'evidence': experience['evidence']})
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger="run_finished")
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": "run_finished",
            },
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                **self._outcome_fields(task_state),
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        return final

    def _drain_events(self, task_state, native):
        agent = self.agent
        while self.pending_events:
            event = self.pending_events.popleft()
            if isinstance(event, str):
                event = {'type': 'user_message', 'content': event}
            if not isinstance(event, dict):
                raise TypeError('Agent events must be strings or dictionaries')
            content = event.get('content', '')
            if not isinstance(content, str) or not content.strip():
                raise ValueError('Agent event content must be a non-empty string')
            content = content.strip()
            event_type = str(event.get('type') or 'user_message')
            agent.record({'role': 'user', 'content': content, 'created_at': now(),
                          'run_id': task_state.run_id, 'event_type': event_type})
            agent.emit_trace(task_state, 'environment_event', {
                'event_type': event_type, 'content': clip(content, 500),
            })
            conversation = getattr(agent.model_client, 'native_conversation', None) if native else None
            if conversation is not None:
                agent.model_client.native_notice('New '+event_type+': '+content)

    def run(self, user_message):
        from .stepwise import AgentRun
        return AgentRun(self.agent).start(user_message).run_to_completion()

    def run_steps(self, user_message):
        agent = self.agent
        agent._run_history_start = len(agent.session.get('history', []))
        if agent.capabilities:
            agent.cancellation.check()
        completion = CompletionGate(agent)
        # Science-constraint gate (P1/P2/P3). Inert unless this turn's instruction
        # states an explicit statistical requirement, so ordinary runs are
        # unaffected. It only rejects completion on positively observed drift.
        scientific = None
        if getattr(agent, 'scientific_verification', True):
            from goaltree.scientific.gate import ScientificGate
            candidate = ScientificGate(user_message)
            scientific = candidate if candidate.enabled else None
        agent._scientific_gate = scientific
        from .response_contract import from_request
        agent.response_contract = from_request(user_message)
        format_repairs = 0
        run_started_at = time.monotonic()
        agent.memory.set_task_summary(user_message)
        previous_working_state = agent.session.get('working_state', {})
        if agent.capabilities:
            from .evidence import invalidate_plan
            invalidated = invalidate_plan(agent)
            if invalidated:
                agent.session['plan_invalidations'] = invalidated
        agent.session['working_state'] = {'goal': user_message[:300], 'changed_files': [],
                                          'failed_attempts': [], 'verification': {'status': 'not_run'}}
        if agent.response_contract:
            agent.session['working_state']['response_contract']={'kind':agent.response_contract.kind,
                'object_required':agent.response_contract.object_required,
                'source':'explicit current user instruction; bounded reply-layout grammar, not a semantic grader'}
        agent.record({"role": "user", "content": user_message, "created_at": now(),
                      **({'attachments': agent.message_attachments} if getattr(agent, 'message_attachments', None) else {})})

        task_state = TaskState.create(run_id=agent.new_run_id(), task_id=agent.new_task_id(), user_request=user_message)
        task_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        agent.current_task_state = task_state
        if agent.capabilities:
            from .session_context import start
            start(agent, task_state.run_id)
        if agent.capabilities:
            agent.capabilities.learning.prepare(user_message)
        agent.current_run_dir = agent.run_store.start_run(task_state)
        if getattr(agent, 'goal_context', {}).get('cooperation'):
            agent.emit_trace(task_state, 'cooperation_configured', agent.goal_context['cooperation'])
        native = (agent.effective_wire_protocol == 'native' if agent.effective_wire_protocol
                  else agent.native_tools_enabled())
        agent.effective_wire_protocol = 'native' if native else 'text'
        if not native and getattr(agent.model_client, 'native_conversation', None) is not None:
            # An explicit text run must not reuse a previous native payload;
            # complete() would otherwise replace the flattened text messages.
            # Slots-only legacy text clients need not expose this attribute.
            agent.model_client.native_conversation = None
        if native:
            import json
            # Do not seed a new run with unbounded, potentially stale raw tool
            # transcripts. Keep user instructions intact and carry structured
            # historical state; fresh source facts arrive through retrieval.
            carry_keys = ('changed_files', 'failed_attempts') if agent.capabilities else ('goal', 'changed_files', 'failed_attempts')
            # Old user requests already have chronological, scoped evidence
            # frames. Repeating their imperative under the current request's
            # pinned workspace text makes expired restrictions look current.
            carryover = {k: previous_working_state[k] for k in carry_keys if k in previous_working_state}
            web=getattr(agent,'web_context',None)
            web_references=web.references() if web else []
            legacy_history = ('\n\n'.join(item['content'] for item in agent.session['history'][:-1] if item.get('role') == 'user')
                              if not agent.capabilities else '')
            begin_native = agent.model_client.begin_native_run
            environment_kwargs = {}
            if agent.environment is not None:
                import inspect
                parameters = inspect.signature(begin_native).parameters
                if ('environment' not in parameters
                        and not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())):
                    raise TypeError('This model client cannot accept EnvironmentProfile; '
                                    'update begin_native_run(..., environment=None) before enabling a native environment')
                environment_kwargs['environment'] = agent.environment
            begin_native(
                agent.tools,
                agent.redact_text(agent.workspace.text() + '\n\n' + (agent.memory_text() if agent.feature_enabled('memory') else 'Memory: disabled') +
                    ('\n\nPrevious user instructions:\n' + legacy_history if legacy_history else '') +
                    '\n\nHistorical task state (not a current verification):\n' + json.dumps(carryover, ensure_ascii=False) +
                    ('\n\nRecent scoped web evidence pointers (historical, untrusted; reuse page_id/URL rather than searching again):\n'
                     +json.dumps(web_references,ensure_ascii=False) if web_references else '')),
                user_message, agent.verification_command, **environment_kwargs)
            conversation = getattr(agent.model_client, 'native_conversation', None)
            if conversation is not None:
                if getattr(agent,'goal_binding',{}).get('mode')=='execute' and hasattr(conversation,'bind_task_executor'):
                    conversation.bind_task_executor()
                from .native_history import historical_turns
                if agent.capabilities:
                    conversation.messages.extend(historical_turns(agent))
                images = [r for r in getattr(agent, 'attachment_rows', []) if r['mime'].startswith('image/')]
                if images:
                    import base64
                    conversation.messages[1]['content'] = [
                        {'type': 'text', 'text': conversation.messages[1]['content']},
                        *[{'type': 'image_url', 'image_url': {'url': 'data:' + r['mime'] + ';base64,' + base64.b64encode(r['data']).decode('ascii')}} for r in images]]
                conversation.context_budget = agent.native_context_policy
                if agent.capabilities:
                    conversation.batch_limit = 8
                    conversation.messages[0]['content'] = conversation.messages[0]['content'].replace(
                        'Send exactly one function call per step.',
                        'You may return at most 8 native calls per request; execution is ordered, budgeted and individually approved. ')
                    if conversation.goal_coordinator:
                        guidance = (' Plan from the user request and current goal. Do not browse local files to guess the topic of a new goal. '
                                    'Ask for the report topic/audience if unknown instead of borrowing another project\'s subject. ')
                    elif getattr(conversation,'task_executor',False):
                        guidance = (' Use evidence and the bound task contract to choose relevant tools. '
                                    'Keep source facts, user requirements and pending manual acceptance distinct. '
                                    'Use evidence IDs for details after compaction. ')
                    else:
                        guidance = (' Use code_search or symbol_info to navigate. '
                                    'Persist a small plan for multi-step tasks. Prose memories stay candidates until operator confirmation. '
                                    'Use evidence IDs to recover details after compaction. Available skills (load only when relevant): ' +
                                    str(agent.capabilities.skills))
                    conversation.messages[0]['content'] += guidance
                    agent.model_client.cancellation = agent.cancellation
        agent.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
                "goal_binding": getattr(agent, 'goal_binding', None),
                "environment": agent.environment.name if agent.environment else None,
                "requested_wire_protocol": agent.requested_wire_protocol,
                "effective_wire_protocol": agent.effective_wire_protocol,
            },
        )

        tool_steps = 0
        attempts = 0
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)
        shared_budget = getattr(agent, 'goal_tool_budget', None)

        def remaining():
            local = agent.max_steps - tool_steps
            return min(local, shared_budget['remaining']) if shared_budget is not None else local

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while remaining() > 0 and attempts < max_attempts:
            self._drain_events(task_state, native)
            if agent.capabilities:
                agent.cancellation.check()
            agent.remaining_tool_steps = remaining()
            conversation = getattr(agent.model_client, 'native_conversation', None) if native else None
            if conversation is not None and hasattr(conversation, 'set_budget'):
                conversation.set_budget(agent.remaining_tool_steps)
            attempts += 1
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )
            if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="freshness_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
            elif prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                agent.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="workspace_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            if prompt_metadata.get("budget_reductions"):
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="context_reduction")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            raw, kind, payload = self._request_model(
                task_state,
                user_message,
                prompt,
                prompt_metadata,
                run_started_at,
                purpose="action",
            )

            if kind in {'tool', 'tools'}:
                actions = payload if kind == 'tools' else [payload]
                step_input = yield AgentStep('action', actions=tuple(actions), task_state=task_state)
                if not isinstance(step_input, StepInput):
                    raise TypeError('AgentRun action input must be StepInput')
                external = self._normalize_observations(actions, step_input.observations) if not step_input.execute_local else None
                results = []
                for index, action in enumerate(actions):
                    if agent.cancellation.event.is_set():
                        if native:
                            while conversation.pending:
                                agent.model_client.observe_native_tool('Not executed: operator cancellation')
                        agent.cancellation.check()
                    if remaining() <= 0:
                        if native:
                            agent.model_client.observe_native_tool('Not executed: tool budget exhausted')
                        agent.emit_trace(task_state, 'tool_not_executed', {'name': action.get('name'), 'reason': 'budget_exhausted'})
                        continue
                    agent.remaining_tool_steps = remaining()
                    tool_steps += 1
                    if step_input.execute_local:
                        tool_result = self._execute_action(action, task_state, completion, native, user_message)
                    else:
                        tool_result = self._observe_external_action(
                            action, external[index], task_state, completion, native, user_message)
                    results.append({'name': action.get('name'), 'args': action.get('args', {}),
                                    'content': tool_result.content, 'metadata': dict(tool_result.metadata)})
                if step_input.execute_local:
                    yield AgentStep('observation', actions=tuple(actions),
                                    observations=tuple(results), task_state=task_state)
                continue

            if kind == "retry":
                if native:
                    agent.model_client.native_notice(payload)
                agent.record({"role": "assistant", "content": payload, "created_at": now()})
                agent.run_store.write_task_state(task_state)
                yield AgentStep('retry', message=payload, task_state=task_state)
                continue

            final = (payload or raw).strip()
            problem = completion.problem() or (agent.goal_sync_problem() if getattr(agent,'goal_sync_problem',None) else '')
            if not problem and scientific is not None:
                problem = scientific.problem(final) or ''
                if problem:
                    agent.emit_trace(task_state, 'scientific_violation', dict(scientific.state))
            layout_problem = self._reply_problem(final) if not problem else None
            if layout_problem:
                format_repairs += 1
                agent.emit_trace(task_state,'response_contract_rejected',{'kind':agent.response_contract.kind,'repair':format_repairs})
                if format_repairs > 2:
                    return self._finish_stopped(task_state,user_message,'response_contract_incomplete',
                        '两次格式纠正后仍未满足本轮JSON输出要求；未修改数值、权限或验收标准。运行记录已保留。',run_started_at)
                problem=layout_problem
            if problem:
                agent.emit_trace(task_state, 'completion_rejected', dict(completion.state))
                agent.record({'role':'assistant', 'content':problem, 'created_at':now()})
                if native:
                    agent.model_client.native_notice(problem)
                if completion.state['status']=='blocked':
                    return self._finish_stopped(task_state, user_message, 'verification_incomplete', problem, run_started_at)
                continue
            return self._finish_success(task_state, user_message, final, run_started_at)

        # Events injected after the last action must still be observed before
        # budget finalization or a terminal retry stop.
        self._drain_events(task_state, native)
        if remaining() <= 0:
            if agent.capabilities:
                agent.cancellation.check()
            task_state.mark_resource_exhausted('tool_budget')
            if native:
                agent.model_client.native_notice('Tool budget exhausted. Do not call tools. Give a concise final answer based on actual evidence.')
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
            prompt += (
                "\n\nRuntime notice: the tool budget is exhausted. Do not call another tool. "
                "Use the evidence already present in the tool history and return exactly one "
                "non-empty <final>...</final> answer."
            )
            prompt_metadata["finalization"] = True
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                    "purpose": "finalization",
                    **self._outcome_fields(task_state),
                },
            )
            for render_attempt in range(3):
                if render_attempt:
                    task_state.record_attempt()
                    agent.run_store.write_task_state(task_state)
                raw, kind, payload = self._request_model(task_state,user_message,prompt,prompt_metadata,
                    run_started_at,purpose="finalization")
                if kind == "final":
                    final = (payload or raw).strip()
                    problem = completion.problem() or (agent.goal_sync_problem() if getattr(agent,'goal_sync_problem',None) else '')
                    if problem:
                        agent.emit_trace(task_state, 'completion_rejected', dict(completion.state))
                        return self._finish_stopped(task_state,user_message,'verification_incomplete',problem,run_started_at)
                    problem=self._reply_problem(final)
                    if not problem:
                        return self._finish_success(task_state,user_message,final,run_started_at)
                    format_repairs += 1
                    agent.emit_trace(task_state,'response_contract_rejected',{'kind':agent.response_contract.kind,'repair':format_repairs})
                    if format_repairs > 2 or render_attempt==2:
                        return self._finish_stopped(task_state,user_message,'response_contract_incomplete',
                            '回复未满足本轮JSON输出要求，已达到有限纠正上限。未执行额外工具或自行验收。',run_started_at)
                    agent.record({'role':'assistant','content':problem,'created_at':now()})
                    if native:agent.model_client.native_notice(problem+' Tool budget is still zero.')
                    prompt += '\n\n' + problem + ' Tool budget is still zero.'
                    continue
                if native and kind in {'tool', 'tools'}:
                    conversation = getattr(agent.model_client, 'native_conversation', None)
                    while conversation and conversation.pending:
                        agent.model_client.observe_native_tool('Not executed: finalization has no tool budget')
                break

        if attempts >= max_attempts and tool_steps < agent.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        return self._finish_stopped(task_state, user_message, task_state.stop_reason, final, run_started_at)

    def _normalize_observations(self, actions, observations):
        if observations is None:
            raise ValueError('External actions require observations; none were provided')
        if isinstance(observations, (list, tuple)):
            values = list(observations)
        elif len(actions) == 1:
            values = [observations]
        else:
            raise ValueError('Multiple actions require a list of observations')
        if len(values) != len(actions):
            raise ValueError(f'Expected {len(actions)} observations, got {len(values)}')
        return values

    def _observe_external_action(self, payload, observation, task_state, completion, native, user_message):
        from .tool_executor import ToolExecutionResult
        if isinstance(observation, dict):
            content = str(observation.get('content', '') or '')
            metadata = dict(observation.get('metadata') or {})
        else:
            content, metadata = str(observation), {}
        from .privacy import CURRENT
        if CURRENT.get():
            CURRENT.get().observe(
                text=content,
                sources=metadata.get('source_paths', []),
                targets=metadata.get('affected_paths', []),
            )
        content, output_metadata = self.agent.tool_executor.preview(payload.get('name', ''), content)
        metadata.update(output_metadata)
        metadata.setdefault('tool_status', 'ok')
        metadata['external_observation'] = True
        return self._execute_action(payload, task_state, completion, native, user_message,
                                    tool_result=ToolExecutionResult(content, metadata))

    def _execute_action(self, payload, task_state, completion, native, user_message, tool_result=None):
        agent = self.agent
        name, args = payload.get('name', ''), payload.get('args', {})
        task_state.record_tool(name)
        if agent.capabilities and args.get('path'):
            try:
                path = agent.path(args['path'])
            except ValueError:
                path = None
            if path is not None:
                agent.capabilities.refresh_rules(path)
                agent.capabilities.active_path = path
        started = time.monotonic()
        if tool_result is None:
            agent.emit_trace(task_state, 'tool_started', {'name': name, 'args': agent.redact_artifact(args)})
            tool_result = agent.execute_tool(name, args)
        else:
            agent.emit_trace(task_state, 'external_observation_received', {
                'name': name, 'args': agent.redact_artifact(args),
                'content': clip(tool_result.content, 500),
            })
        completion.observe(name, args, tool_result.metadata)
        scientific = getattr(agent, '_scientific_gate', None)
        if scientific is not None:
            scientific.observe(name, args, tool_result.content)
        result = tool_result.content
        command = getattr(agent, 'verification_command', None)
        if (getattr(agent, 'auto_verify_after_write', False) and command
                and name in {'write_file', 'patch_file', 'apply_changes'}
                and tool_result.metadata.get('tool_status') == 'ok'
                and not getattr(agent, 'read_only', False)):
            # Small-model scaffold: the host runs the *real* verification command
            # after each successful edit and shows the actual output in the same
            # observation. Requirement unchanged: completion still needs exit 0.
            verify_args = {'command': command, 'timeout': 60}
            agent.emit_trace(task_state, 'auto_verify_started',
                             {'command': command, 'after': name})
            started_verify = time.monotonic()
            verify = agent.execute_tool('run_shell', verify_args)
            completion.observe('run_shell', verify_args, verify.metadata)
            if scientific is not None:
                scientific.observe('run_shell', verify_args, verify.content)
            result += (f"\n\n[AUTO-VERIFY exit={verify.metadata.get('exit_code')}]\n"
                       f"{verify.content}")
            agent.emit_trace(task_state, 'auto_verify_executed', {
                'command': command, 'exit_code': verify.metadata.get('exit_code'),
                'duration_ms': int((time.monotonic() - started_verify) * 1000)})
        working = agent.session['working_state']
        working['verification'] = dict(completion.state)
        working['changed_files'] = sorted(set(working['changed_files']) | set(tool_result.metadata.get('affected_paths', [])))
        if agent.capabilities:
            from .session_context import observe
            observe(agent, name, args, tool_result, completion.state)
        if tool_result.metadata.get('tool_status') != 'ok':
            working['failed_attempts'].append({'tool': name, 'path': args.get('path'),
                'error': tool_result.metadata.get('tool_error_code'), 'evidence': result[:400]})
            working['failed_attempts'] = working['failed_attempts'][-6:]
        if native:
            agent.model_client.observe_native_tool(result)
        agent.record({'role': 'tool', 'name': name, 'args': args, 'content': result, 'created_at': now(),
                      'source_paths': tool_result.metadata.get('source_paths', [])})
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(task_state, 'tool_executed', {'name': name, 'args': args,
            'result': clip(result, 12000 if name == 'run_shell' else 500),
            'duration_ms': int((time.monotonic()-started)*1000), **dict(tool_result.metadata or {})})
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger='tool_executed')
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(task_state, 'checkpoint_created', {'checkpoint_id': checkpoint['checkpoint_id'], 'trigger': 'tool_executed'})
        return tool_result

    def _finish_stopped(self, task_state, user_message, reason, final, run_started_at):
        agent = self.agent
        task_state.stop(reason, final_answer=final)
        agent.record({"role": "assistant", "content": final, "created_at": now(), 'run_id': task_state.run_id})
        agent.run_store.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger=task_state.stop_reason or "run_stopped")
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": task_state.stop_reason or "run_stopped",
            },
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                **self._outcome_fields(task_state),
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        return final

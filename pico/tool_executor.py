"""Structured tool execution for the agent runtime."""

from dataclasses import dataclass
import re
import subprocess
import json

from .workspace import clip
from .operations import RecoveryRequired


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict


def _metadata(
    tool_status,
    tool_error_code="",
    security_event_type="",
    risk_level="low",
    read_only=True,
    affected_paths=None,
    workspace_changed=False,
    workspace_fingerprint="",
    diff_summary=None,
):
    result = {
        "tool_status": tool_status,
        "tool_error_code": tool_error_code,
        "security_event_type": security_event_type,
        "risk_level": risk_level,
        "read_only": read_only,
        "affected_paths": list(affected_paths or []),
        "workspace_changed": bool(workspace_changed),
        "diff_summary": list(diff_summary or []),
    }
    if workspace_fingerprint:
        result["workspace_fingerprint"] = workspace_fingerprint
    return result


class ToolExecutor:
    def __init__(self, agent):
        self.agent = agent

    def preview(self, name, raw_content):
        """Build the shared redacted preview for local or external results."""
        agent = self.agent
        output_limit = (agent.web_budget.result_limit if name in {'web_read', 'web_find', 'web_search'}
                        and hasattr(agent, 'web_budget') else 12000 if name == 'run_shell'
                        else int(agent.tools[name].get('output_limit', 4000)))
        raw_text = str(raw_content)
        safe_text = agent.redact_text(raw_text)
        content = clip(safe_text, output_limit)
        metadata = {
            'output_total_chars': len(safe_text),
            'output_raw_chars': len(raw_text),
            'output_truncated': len(safe_text) > output_limit,
            'output_limit_chars': output_limit,
        }
        if len(safe_text) > output_limit and name != 'read_tool_output':
            descriptor = agent.store_tool_output(safe_text, name)
            if descriptor:
                next_offset = min(output_limit + 1, descriptor['stored_chars'] + 1)
                notice = (
                    f"Output ID: {descriptor['output_id']}"
                    f"\nOriginal output has {descriptor['total_chars']} characters; "
                    f"{descriptor['stored_chars']} are stored for pagination. "
                    f"Continue with read_tool_output(output_id={descriptor['output_id']!r}, "
                    f"offset={next_offset}, limit=4000). This is host audit data, not a new environment action."
                )
                if descriptor['stored_truncated']:
                    notice += ' Content beyond the storage cap is unavailable; use a narrower environment query.'
                content = notice + '\n' + content
                metadata.update(
                    output_id=descriptor['output_id'],
                    output_next_offset=next_offset,
                    output_stored_chars=descriptor['stored_chars'],
                    output_stored_truncated=descriptor['stored_truncated'],
                )
        metadata['output_preview_chars'] = len(content)
        return content, metadata

    def execute(self, name, args):
        agent = self.agent
        if getattr(agent, 'harness_profile', 'classic') == 'workbench':
            agent.cancellation.check()
        if (name in {'write_file','patch_file', 'apply_changes', 'rewind_changes'} and getattr(agent,'verification_command',None)
                and getattr(agent.model_client,'supports_native_tools',False)
                and getattr(agent,'remaining_tool_steps',None)==1):
            return ToolExecutionResult(content='error: last tool step is reserved for the exact verification command; no edit applied',
                metadata=_metadata('rejected',tool_error_code='verification_budget_reserved',risk_level='high',read_only=False))
        if agent.allowed_tools is not None and name not in agent.allowed_tools:
            return ToolExecutionResult(
                content=f"error: tool '{name}' is not allowed in this run",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="tool_not_allowed",
                    risk_level="high",
                    read_only=False,
                ),
            )

        tool = agent.tools.get(name)
        if tool is None:
            return ToolExecutionResult(
                content=f"error: unknown tool '{name}'",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="unknown_tool",
                    risk_level="high",
                    read_only=False,
                ),
            )

        try:
            agent.validate_tool(name, args)
            approval_args=args
            if name=='confirm_task' and getattr(agent,'goal_confirmation_preview',None):
                approval_args=agent.goal_confirmation_preview(args)
        except Exception as exc:
            example = agent.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if name == "patch_file":
                # A mismatched XML closing tag can merge both texts into one
                # argument. Reject it, then offer an unambiguous JSON repair.
                message += "\nKeep old_text and new_text as separate JSON fields; do not place XML tags inside either field."
                message += " Escape line breaks in JSON strings. Return one corrected JSON tool call or a final answer."
                example = "<tool>" + json.dumps({"name": "patch_file", "args": {
                    "path": "file.py", "old_text": "exact old text", "new_text": "replacement text",
                }}) + "</tool>"
                message += '\nNo edit was applied. Read the current target range once, copy exact old_text without line numbers, and retry with a smaller unique block. Preserve newlines and indentation.'
            if getattr(agent.model_client, 'supports_native_tools', False):
                example = ''
                message = message.replace('Return one corrected JSON tool call or a final answer.',
                                          'Use a corrected native function call.')
            if example:
                message += f"\nexample: {example}"
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else "sensitive_path" if "sensitive path denied" in str(exc) else ""
            return ToolExecutionResult(
                content=message,
                metadata=_metadata(
                    "rejected",
                    tool_error_code="invalid_arguments",
                    security_event_type=security_event_type,
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            )

        if agent.repeated_tool_call(name, args):
            return ToolExecutionResult(
                content=(f"error: repeated identical tool call for {name}; no new information. "
                         "Use the previous result. For clipped reads request only the unread line range; "
                         "for a rejected patch correct old_text from current source; for failing tests fix code before rerunning."),
                metadata=_metadata(
                    "rejected",
                    tool_error_code="repeated_identical_call",
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            )

        operation_id = None
        # Optional GoalTree collaboration gates run after argument/scope checks.
        # Risky tools still require the original safety approval below.
        checkpoint = getattr(agent, 'collaboration_gate', None)
        try:
            consent = checkpoint(name, args, tool['risky']) if checkpoint else True
        except (ValueError, TypeError, KeyError) as exc:
            return ToolExecutionResult('error: invalid collaboration proposal: ' + str(exc),
                                       _metadata('rejected', tool_error_code='invalid_arguments'))
        if not consent:
            return ToolExecutionResult('error: collaboration confirmation denied; no action was applied. '
                                       'Respect the decision and ask for revised requirements; do not bypass it with another tool.',
                                       _metadata('rejected', tool_error_code='collaboration_denied'))
        observe_effects = tool['risky'] or tool.get('observes_effects', False)
        before_snapshot = agent.capture_workspace_snapshot() if observe_effects else {}
        if tool['risky']:
            try:
                conversation = getattr(agent.model_client, 'native_conversation', None)
                state = getattr(agent, 'current_task_state', None)
                operation_id, cached = agent.operations.begin(name, args, before_snapshot,
                    agent.session['id'], state.run_id if state else '', getattr(conversation, 'pending', None))
                if cached is not None:
                    return ToolExecutionResult(cached['content'], {**cached['metadata'], 'operation_result_reused': True})
            except RecoveryRequired as exc:
                return ToolExecutionResult('error: ' + str(exc), _metadata('rejected',
                    tool_error_code='operation_recovery_required', risk_level='high', read_only=False))
        if tool["risky"] and not agent.approve(name, approval_args):
            agent.operations.reject(operation_id)
            detail = (f"; use the exact configured verification command {agent.verification_command!r}; "
                      "do not try pipes or alternative commands" if name=='run_shell' and agent.verification_command else '')
            return ToolExecutionResult(
                content=f"error: approval denied for {name}"+detail,
                metadata=_metadata(
                    "rejected",
                    tool_error_code="approval_denied",
                    security_event_type="read_only_block" if agent.read_only else "approval_denied",
                    risk_level="high",
                    read_only=False,
                ),
            )

        if tool['risky']:
            if getattr(agent, 'capabilities', None):
                if agent.cancellation.event.is_set():
                    agent.operations.reject(operation_id)
                    agent.cancellation.check()
                try:
                    agent.capabilities.hooks.before(name, args)
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    agent.operations.reject(operation_id)
                    return ToolExecutionResult(str(exc), _metadata('rejected', tool_error_code='policy_hook_denied',
                        security_event_type='policy_denied', risk_level='high', read_only=False))
            try:
                agent.operations.start(operation_id, agent.capture_workspace_snapshot())
            except RecoveryRequired as exc:
                return ToolExecutionResult('error: ' + str(exc), _metadata('rejected',
                    tool_error_code='approval_snapshot_changed', risk_level='high', read_only=False))
        after_snapshot = before_snapshot
        try:
            raw_content = tool['run'](args)
            from .privacy import CURRENT
            if CURRENT.get():CURRENT.get().observe(text=raw_content)
            if getattr(agent, 'evidence', None):
                agent._raw_tool_evidence = agent.redact_text(clip(raw_content, 131072))
            content, output_metadata = self.preview(name, raw_content)
            after_snapshot = agent.capture_workspace_snapshot() if observe_effects else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            tool_status = "ok"
            tool_error_code = ""
            if name == "run_shell":
                match = re.search(r"exit_code:\s*(-?\d+)", content)
                exit_code = int(match.group(1)) if match else 0
                if exit_code != 0 and workspace_changed:
                    tool_status = "partial_success"
                    tool_error_code = "tool_partial_success"
                elif exit_code != 0:
                    tool_status = "error"
                    tool_error_code = "tool_failed"
            agent.update_memory_after_tool(name, args, content)
            metadata = _metadata(
                tool_status,
                tool_error_code=tool_error_code,
                risk_level="high" if tool["risky"] or workspace_changed else "low",
                read_only=not tool["risky"] and not workspace_changed,
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
            )
            metadata.update(
                **({'effect': tool['effect']} if 'effect' in tool else {}),
                **output_metadata,
            )
            if name == "run_shell":
                metadata["exit_code"] = int(match.group(1)) if match else None
                metadata["execution_backend"] = getattr(agent, 'shell_backend', 'host')
            if name == 'web_search':
                search = json.loads(raw_content)
                metadata['search_status'] = search['status']
                metadata['search_audit_evidence_id'] = search.get('audit_evidence_id')
                metadata['search_sources'] = [{k: row[k] for k in ('source','status','count','error','failure_type','low_query_overlap','max_query_overlap') if k in row}
                                             for row in search['attempts']]
                if search['status'] == 'partial':
                    metadata.update(tool_status='partial_success', tool_error_code='search_source_failed')
                metadata['web_cache'] = search.get('cache')
                metadata['web_budget'] = search.get('web_budget')
            if name in {'web_read','web_find'}:
                page=json.loads(raw_content)
                metadata.update(web_page_id=page['page_id'],web_cache=page.get('cache','snapshot'),
                    web_citation=page.get('citation') or next((m['citation'] for m in page.get('matches',[])),None),web_budget=page.get('web_budget'))
                if page.get('document_type')=='pdf':
                    metadata.update(document_type='pdf',pdf_page_count=page['page_count'],pdf_text_status=page['text_extraction_status'],
                                    pdf_start_page=page.get('start_page'),pdf_end_page=page.get('end_page'))
                    if page['text_extraction_status']!='text_extracted':
                        metadata.update(tool_status='partial_success',tool_error_code='pdf_text_incomplete')
            if operation_id:
                metadata['operation_id'] = operation_id
                agent.operations.complete(operation_id, after_snapshot, content, metadata)
            agent.record_process_note_for_tool(name, metadata)
            return ToolExecutionResult(content=content, metadata=metadata)
        except Exception as exc:
            after_snapshot = agent.capture_workspace_snapshot() if observe_effects else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else "sensitive_path" if "sensitive path denied" in str(exc) else ""
            metadata = _metadata(
                "partial_success" if workspace_changed else "error",
                tool_error_code="tool_partial_success" if workspace_changed else "tool_failed",
                security_event_type=security_event_type,
                risk_level="high" if tool["risky"] or workspace_changed else "low",
                read_only=not tool["risky"] and not workspace_changed,
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
            )
            if isinstance(exc, subprocess.TimeoutExpired):
                metadata["tool_error_code"] = "tool_timeout"
                metadata["timed_out"] = True
            if exc.__class__.__name__ == 'Cancelled':
                metadata['tool_error_code'] = 'cancelled'
            if getattr(exc,'web_budget_exhausted',False):
                metadata.update(tool_error_code='web_budget_exhausted',web_budget_stop=True)
            if getattr(exc,'pdf_error',False):metadata['tool_error_code']='pdf_read_failed'
            if name == 'web_search' and getattr(exc, 'search_report', None):
                metadata['search_audit_evidence_id'] = getattr(exc,'audit_evidence_id',None)
                metadata['tool_error_code'] = 'web_search_failed'
                metadata['search_status'] = 'error'
                metadata['search_sources'] = [{k: row[k] for k in ('source','status','count','error','failure_type') if k in row}
                                             for row in exc.search_report['attempts']]
            if name == 'run_shell':
                metadata['execution_backend'] = getattr(agent, 'shell_backend', 'host')
                if exc.__class__.__name__ == 'SandboxUnavailable':
                    metadata['tool_error_code'] = 'sandbox_unavailable'
            if operation_id:
                metadata['operation_id'] = operation_id
                agent.operations.complete(operation_id, after_snapshot, agent.redact_text(f'error: tool {name} failed: {exc}'), metadata,
                                          uncertain=name == 'run_shell' or name.startswith('mcp_'))
            agent.record_process_note_for_tool(name, metadata)
            return ToolExecutionResult(content=f"error: tool {name} failed: {exc}", metadata=metadata)

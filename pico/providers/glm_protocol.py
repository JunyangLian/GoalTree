"""Recognize only explicitly measured GLM zero-reasoning turns.

Thinking remains enabled. Existing reasoning text is never removed, invented or
rewritten. A missing field with unknown/positive reasoning usage is not exempt.
"""


def measured_zero_reasoning(message, usage):
    details = usage.get('completion_tokens_details') or {}
    tokens = details.get('reasoning_tokens')
    return message.get('reasoning_content') is None and type(tokens) is int and tokens == 0

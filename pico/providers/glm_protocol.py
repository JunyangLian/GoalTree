"""GLM reasoning observation and optional-field validation.

Preserve every returned reasoning string unchanged. Token accounting cannot
reconstruct missing text or establish that an optional response field exists.
Never synthesize reasoning, disable thinking, or relax tool authorization.
"""


def measured_zero_reasoning(message, usage):
    details = usage.get('completion_tokens_details') or {}
    tokens = details.get('reasoning_tokens')
    return message.get('reasoning_content') is None and type(tokens) is int and tokens == 0


def valid_optional_reasoning(message):
    value = message.get('reasoning_content')
    return value is None or isinstance(value, str)

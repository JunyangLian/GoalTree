"""Small explicit reply-layout grammar. No inferred facts or benchmark gold.

Recognizes direct JSON-only / JSON-at-end instructions in the current request.
It is deliberately not a general natural-language intent classifier.
"""
import json
import re
from dataclasses import dataclass


def _valid_json(text, object_required=True):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('nonfinite JSON')
    value = json.loads(text, object_pairs_hook=unique, parse_constant=constant)
    return isinstance(value, dict) if object_required else True


@dataclass(frozen=True)
class ResponseContract:
    kind: str
    object_required: bool = True

    def problem(self, answer):
        text = answer.strip()
        valid = False
        try:
            if self.kind == 'json_only':
                valid = _valid_json(text, self.object_required)
            else:
                blocks = list(re.finditer(r'```([^\n]*)\n(.*?)\n```', text, re.S))
                if blocks and not text[blocks[-1].end():].strip():
                    last = blocks[-1]
                    valid = last.group(1).strip().lower() in {'', 'json'} and _valid_json(last.group(2), self.object_required)
                elif not text.endswith('```'):
                    # Bounded scan; never rewrite content or look for an answer
                    # matching a key/value. Validate a whole object at the end.
                    decoder = json.JSONDecoder()
                    for match in list(re.finditer(r'[\{\[]', text))[-1000:]:
                        try:
                            value, end = decoder.raw_decode(text, match.start())
                            if isinstance(value, (dict, list)) and not text[end:].strip():
                                valid = _valid_json(text[match.start():end], self.object_required)
                                break
                        except ValueError:
                            continue
        except (ValueError, TypeError, RecursionError):
            valid = False
        if valid:
            return None
        value_kind = 'JSON object' if self.object_required else 'JSON value'
        layout = ('one raw '+value_kind+' only, without Markdown fences or any surrounding prose' if self.kind == 'json_only'
                  else 'one valid '+value_kind+' at the very end (a JSON code fence is allowed); put every caveat/explanation BEFORE it')
        return ('Reply layout does not satisfy the current user instruction. Return ' + layout +
                '. Keep the actual verified values and current delivery requirements; do not invent or change facts to repair layout. '
                'Do not call tools just to fix layout, change permissions, accept tasks or erase failures.')


def from_request(request):
    # Only the caller's current request, never a file, tool result or old chat.
    text = re.sub(r'```.*?```', '', request, flags=re.S)
    text = re.sub(r'`[^`\n]*`|“[^”\n]*”|"[^"\n]*"', '', text)
    only = (r'(?:只|仅)(?:需|要)?(?:返回|输出|回答|给出|提供)(?:一个|有效的|合法的|纯)?\s*JSON|'
            r'\b(?:return|respond|reply|output|provide)\s+(?:with\s+)?(?:only|just)\s+(?:a\s+)?(?:valid\s+)?JSON\b|'
            r'\b(?:return|reply|output)\s+(?:with\s+)?JSON\s+only\b')
    tail = (r'(?:末尾|最后|结尾)[^。！？!?;\n]{0,30}?(?:返回|输出|给出|提供|附上|放置|添加|加上)[^。！？!?;\n]{0,20}?JSON|'
            r'(?:把|将)\s*(?:一个)?JSON(?:对象)?[^。！？!?;\n]{0,8}?(?:放|置)[^。！？!?;\n]{0,12}?(?:末尾|最后|结尾)|'
            r'\b(?:end|finish|conclude)\b[^.!?;\n]{0,60}?\b(?:with|in)\s+(?:a\s+)?JSON\b|'
            r'\bJSON(?:\s+object)?\s+at\s+the\s+end\b')
    matches = []
    for part in re.finditer(r'[^。！？!?;\n]+', text):
        clause = part.group(0)
        if re.match(r'\s*(?:请)?(?:解释|讨论|说明这句话|什么是|为什么|如何)|\s*(?:explain|what is|why)\b', clause, re.I):
            continue
        for kind, pattern in (('json_only', only), ('json_tail', tail)):
            for match in re.finditer(pattern, clause, re.I):
                if (re.search(r'(?:不要|不必|无需|不需要|不用|不得|not|never)\s*$', clause[:match.start()], re.I)
                        or re.search(r'不要|不必|无需|不需要|不用|不得|\bnot\b|\bnever\b', match.group(0), re.I)):
                    continue
                object_required = bool(re.search(r'JSON\s*(?:对象|object\b)', clause, re.I))
                matches.append((part.start() + match.start(), kind, object_required))
    return ResponseContract(*max(matches)[1:]) if matches else None

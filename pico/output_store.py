"""Bounded, session-scoped original tool output for exact offset pagination.

Runtime writes redacted content here only when a model preview is truncated.
The store is intentionally separate from Workbench EvidenceStore: classic
external environments need pagination without acquiring the full capability
stack. Files are local audit data, not a sandbox or a cross-session API.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


MAX_STORED_CHARS = 131072
MAX_OUTPUTS_PER_SESSION = 40
MAX_READ_CHARS = 12000
_OUTPUT_ID = re.compile(r"out_[0-9a-f]{16}")


class ToolOutputStore:
    def __init__(self, workspace_root, session_id):
        session_hash = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:24]
        self.root = Path(workspace_root) / ".pico" / "tool_outputs" / session_hash

    def _path(self, output_id):
        if not isinstance(output_id, str) or not _OUTPUT_ID.fullmatch(output_id):
            raise ValueError("Invalid tool output ID")
        return self.root / f"{output_id}.json"

    def put(self, content, *, tool, run_id, total_chars=None):
        """Store at most MAX_STORED_CHARS and return an output descriptor.

        Content must already be redacted by the runtime. Existing IDs are never
        overwritten. A full session store returns None instead of evicting an
        output that may already be referenced by model history.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        if len(list(self.root.glob("out_*.json"))) >= MAX_OUTPUTS_PER_SESSION:
            return None
        text = str(content)
        stored = text[:MAX_STORED_CHARS]
        output_id = "out_" + uuid.uuid4().hex[:16]
        original_chars = len(text) if total_chars is None else int(total_chars)
        payload = {
            "schema": "tool-output-v1",
            "output_id": output_id,
            "tool": str(tool),
            "run_id": str(run_id or ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "total_chars": original_chars,
            "stored_chars": len(stored),
            "stored_truncated": len(stored) < original_chars,
            "content": stored,
        }
        path = self._path(output_id)
        temporary = self.root / f".{output_id}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {
            "output_id": output_id,
            "total_chars": original_chars,
            "stored_chars": len(stored),
            "stored_truncated": len(stored) < original_chars,
        }

    def read(self, output_id, *, offset=1, limit=4000):
        if type(offset) is not int or offset < 1:
            raise ValueError("offset must be a positive integer")
        if type(limit) is not int or not 1 <= limit <= MAX_READ_CHARS:
            raise ValueError(f"limit must be in 1..{MAX_READ_CHARS}")
        path = self._path(output_id)
        if not path.is_file():
            raise ValueError("Unknown output ID in the current session")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != "tool-output-v1":
            raise ValueError("Unsupported tool output schema")
        content = str(payload["content"])
        if offset > len(content) + 1:
            raise ValueError(f"offset exceeds stored output ({len(content)} characters)")
        text = content[offset - 1:offset - 1 + limit]
        next_offset = offset + len(text)
        has_more = next_offset <= len(content)
        return {
            "output_id": output_id,
            "tool": payload["tool"],
            "run_id": payload["run_id"],
            "offset": offset,
            "returned_chars": len(text),
            "next_offset": next_offset if has_more else None,
            "has_more": has_more,
            "total_chars": payload["total_chars"],
            "stored_chars": payload["stored_chars"],
            "stored_truncated": payload["stored_truncated"],
            "text": text,
        }

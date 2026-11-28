"""Evidence primitives shared by the verifier and recovery loops."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Evidence:
    kind: str            # result_table | script | command | provenance | sample_list
    path: str | None = None
    hash: str | None = None
    content: str | None = None

    @classmethod
    def from_text(cls, kind: str, content: str, path: str | None = None) -> "Evidence":
        return cls(kind=kind, path=path, hash=hash_text(content), content=content)
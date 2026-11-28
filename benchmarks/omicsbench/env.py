"""Execution environment boundary for the OmicsBench adapter.

The adapter never executes tools itself; it maps a GoalTree tool Action to an
``Environment`` and treats only the returned string as the Observation. A real
run replaces ``MockEnvironment`` with an executor that runs commands in the
official container (``/data`` read-only, ``/workspace`` writable, no network).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


class Environment:
    """Pluggable tool-execution boundary. Concrete subclasses do the real work."""

    # "ok" or "error" for the most recent execution. Scientific verification must
    # only treat *successful* commands as evidence: a malformed command that
    # failed still contains text like ``padj < 0.05`` and would otherwise be read
    # as method drift that never happened.
    last_status: str = "ok"

    def execute(self, name: str, args: dict[str, Any]) -> str:
        raise NotImplementedError

    def read_answer(self, filename: str) -> str | None:
        raise NotImplementedError

    def clear_answer(self, filename: str) -> None:
        """Remove any pre-existing graded answer file for ``filename``.

        A node must only be scored on what it writes during that node; a stale
        file left by an earlier run that reused the same work root must not count.
        The default is a no-op so custom environments keep working; concrete
        environments override it.
        """
        return None


def _answers_path(filename: str) -> str:
    return f"/workspace/answers/{filename}"


class MockEnvironment(Environment):
    """In-memory environment for offline adapter tests. No real bioinformatics.

    Supports the same tool names GoalTree exposes (``write_file``, ``read_file``,
    ``run_shell``, ``list_files``) using a dict-backed filesystem keyed by the
    path the agent passed. ``run_shell`` is intentionally inert: it records the
    command and returns a fixed message, so a mock cannot be mistaken for a real
    executed analysis.
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.tool_calls: list[dict[str, Any]] = []

    def execute(self, name: str, args: dict[str, Any]) -> str:
        self.last_status = "ok"
        self.tool_calls.append({"name": name, "args": dict(args)})
        if name == "write_file":
            path = str(args.get("path", "")).strip()
            content = args.get("content")
            if not path:
                self.last_status = "error"
                return "error: write_file requires a non-empty 'path' argument."
            if content is None or content == "":
                self.last_status = "error"
                return "error: write_file requires a non-empty 'content' argument."
            content = str(content)
            self.files[path] = content
            if path.startswith("/workspace/answers/"):
                self.files[path.removeprefix("/workspace/answers/")] = content
            elif path.startswith("/workspace/"):
                self.files[path.removeprefix("/workspace/")] = content
            return f"wrote {len(content)} chars to {path}"
        if name == "read_file":
            path = str(args.get("path", ""))
            if path in self.files:
                return self.files[path]
            self.last_status = "error"
            return f"error: no such file {path}"
        if name == "run_shell":
            command = str(args.get("command", ""))
            return f"[mock shell] command not executed in offline environment: {command}"
        if name == "list_files":
            return "\n".join(sorted(self.files))
        return f"[mock] unsupported tool: {name}"

    def read_answer(self, filename: str) -> str | None:
        return self.files.get(_answers_path(filename)) or self.files.get(filename)

    def clear_answer(self, filename: str) -> None:
        self.files.pop(_answers_path(filename), None)
        self.files.pop(filename, None)


class HostEnvironment(Environment):
    """Real local-shell execution for Windows development (NOT official Docker).

    Runs ``run_shell`` on the host via ``subprocess``, and maps the OmicsBench
    container paths (``/data``, ``/workspace``/``answers``/``scratch``) onto a
    local root. Linux container paths inside a command string are rewritten to
    the local Windows paths before execution. This is a development shim used to
    test the science on real data without Docker; it is not the paper condition.
    """

    # Common Linux-only binaries that have no default Windows equivalent. The
    # agent tends to reach for these on a "Linux container" prompt; short-circuit
    # them with an actionable hint instead of letting cmd's opaque error loop.
    _LINUX_ONLY = {
        "zcat", "sed", "awk", "grep", "wc", "sort", "uniq", "cut",
        "head", "tail", "ls", "cat", "rm", "cp", "mv", "chmod", "git",
    }

    def _linux_hint(self, command: str) -> str | None:
        stripped = command.strip()
        if not stripped:
            return None
        first = stripped.split()[0]
        if first in self._LINUX_ONLY:
            return (
                f"[not executed] '{first}' is a Linux shell tool that is unavailable on this host. "
                "Use Python instead, e.g. `python -c \"import pandas as pd; df=pd.read_csv('/data/<file>')\"` "
                "to inspect or analyze the data file directly."
            )
        if "<<" in command:
            return (
                "[not executed] heredoc ('<<') redirection is not supported by this host's shell. "
                "Write the script with the write_file tool, then run `python <script-path>`."
            )
        return None

    # Script file types materialised on the host: their *content* also refers to
    # container paths (/data, /workspace/...) and must be translated, otherwise
    # `python scratch/x.py` fails with FileNotFoundError and the agent burns its
    # whole step budget debugging the shim instead of doing the analysis.
    _SCRIPT_SUFFIXES = {".py", ".sh", ".r"}

    def __init__(self, data_root: str | Path, work_root: str | Path) -> None:
        self.data_root = Path(data_root)
        self.work_root = Path(work_root)
        (self.work_root / "answers").mkdir(parents=True, exist_ok=True)
        (self.work_root / "scratch").mkdir(parents=True, exist_ok=True)
        self.tool_calls: list[dict[str, Any]] = []

    def _resolve(self, path: str) -> Path:
        s = str(path).replace("\\", "/")
        for virt, root in (
            ("/workspace/answers", self.work_root / "answers"),
            ("/workspace/scratch", self.work_root / "scratch"),
            ("/workspace", self.work_root),
            ("/data", self.data_root),
        ):
            if s == virt:
                return Path(root)
            if s.startswith(virt + "/"):
                return Path(root) / s[len(virt) + 1:]
        return self.work_root / s.lstrip("/")

    def _translate(self, command: str) -> str:
        # Replace both "/data" and "/data/..." and bare "/workspace". More specific
        # prefixes first. Without bare-path handling, `os.listdir('/data')` stays
        # untranslated, resolves to nowhere on Windows, and the agent falls into a
        # whole-filesystem search that burns the wall clock.
        out = command
        for virt, real in (
            ("/workspace/answers", self.work_root / "answers"),
            ("/workspace/scratch", self.work_root / "scratch"),
            ("/workspace", self.work_root),
            ("/data", self.data_root),
        ):
            out = out.replace(virt, str(real).replace("\\", "/"))
        return out

    def execute(self, name: str, args: dict[str, Any]) -> str:
        self.last_status = "ok"
        self.tool_calls.append({"name": name, "args": dict(args)})
        if name == "run_shell":
            command = str(args.get("command", ""))
            hint = self._linux_hint(command)
            if hint:
                self.last_status = "error"
                return hint
            timeout = int(args.get("timeout", 60))
            translated = self._translate(command)
            try:
                proc = subprocess.run(
                    translated,
                    cwd=str(self.work_root),
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    encoding="utf-8",
                    errors="replace",
                )
            except subprocess.TimeoutExpired:
                self.last_status = "error"
                return f"[command timed out after {timeout}s]"
            if proc.returncode != 0:
                self.last_status = "error"
            out = (proc.stdout or "") + (proc.stderr or "")
            return out.strip() or "(no output)"
        if name == "write_file":
            path_arg = str(args.get("path", "")).strip()
            content = args.get("content")
            if not path_arg:
                self.last_status = "error"
                return "error: write_file requires a non-empty 'path' argument."
            if content is None or content == "":
                self.last_status = "error"
                return "error: write_file requires a non-empty 'content' argument."
            p = self._resolve(path_arg)
            p.parent.mkdir(parents=True, exist_ok=True)
            content = str(content)
            if p.suffix.lower() in self._SCRIPT_SUFFIXES:
                # Only scripts are translated: answer JSON must stay byte-identical.
                content = self._translate(content)
            p.write_text(content, encoding="utf-8")
            return f"wrote {len(content)} chars to {p}"
        if name == "read_file":
            p = self._resolve(args.get("path", ""))
            if not p.exists():
                self.last_status = "error"
                return f"error: no such file {p}"
            return p.read_text(encoding="utf-8", errors="replace")
        if name == "list_files":
            p = self._resolve(args.get("path", "."))
            if not p.exists():
                return f"error: no such path {p}"
            if p.is_file():
                return str(p)
            return "\n".join(sorted(x.name for x in p.iterdir()))
        return f"[unsupported tool: {name}]"

    def read_answer(self, filename: str) -> str | None:
        p = self.work_root / "answers" / filename
        return p.read_text(encoding="utf-8") if p.exists() else None

    def clear_answer(self, filename: str) -> None:
        p = self.work_root / "answers" / filename
        if p.exists():
            p.unlink()


class DockerEnvironment(Environment):
    """Runs ``run_shell`` inside a Linux container with the official layout.

    ``/data`` is the host data root mounted **read-only**, ``/workspace`` is the
    work root (writable). File tools operate directly on the host mounts, so
    answers stay byte-identical; only command execution goes through the
    container. Unlike :class:`HostEnvironment` no path translation is needed:
    the container really has ``/data`` and ``/workspace``.

    This is *not* the official pinned OmicsBench image (that one is built by
    their Harbor orchestrator); it is a minimal Linux runtime that removes the
    Windows-shell friction and makes script paths behave as in a container.
    """

    def __init__(self, data_root: str | Path, work_root: str | Path,
                 image: str = "omicsbench-runtime:dev",
                 docker: str | None = None, network: bool = True) -> None:
        self.data_root = Path(data_root)
        self.work_root = Path(work_root)
        (self.work_root / "answers").mkdir(parents=True, exist_ok=True)
        (self.work_root / "scratch").mkdir(parents=True, exist_ok=True)
        self.tool_calls: list[dict[str, Any]] = []
        self.image = image
        self.network = bool(network)
        self._seq = 0
        self._docker = docker or os.environ.get("OMICSBENCH_DOCKER") or ""
        if not self._docker:
            self._docker = shutil.which("docker") or r"D:\Docker\resources\bin\docker.exe"

    def _mount(self, path: Path) -> str:
        return str(path.resolve()).replace("\\", "/")

    def _resolve(self, path: str) -> Path:
        s = str(path).replace("\\", "/")
        for virt, root in (
            ("/workspace", self.work_root),
            ("/data", self.data_root),
        ):
            if s == virt:
                return Path(root)
            if s.startswith(virt + "/"):
                return Path(root) / s[len(virt) + 1:]
        return self.work_root / s.lstrip("/")

    def execute(self, name: str, args: dict[str, Any]) -> str:
        self.last_status = "ok"
        self.tool_calls.append({"name": name, "args": dict(args)})
        if name == "run_shell":
            command = str(args.get("command", ""))
            if not command.strip():
                self.last_status = "error"
                return "error: run_shell requires a non-empty 'command' argument."
            timeout = int(args.get("timeout", 60))
            self._seq += 1
            cname = f"ob_{os.getpid()}_{self._seq}"
            cmd = [self._docker, "run", "--rm", "--name", cname,
                   "-v", f"{self._mount(self.data_root)}:/data:ro",
                   "-v", f"{self._mount(self.work_root)}:/workspace",
                   "-w", "/workspace"]
            if not self.network:
                cmd += ["--network", "none"]
            cmd += [self.image, "sh", "-lc", command]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                      encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                # Killing the client would leave the container running; remove it.
                subprocess.run([self._docker, "rm", "-f", cname],
                               capture_output=True, text=True)
                self.last_status = "error"
                return f"[command timed out after {timeout}s]"
            except OSError as exc:
                self.last_status = "error"
                return f"error: cannot run docker ({exc})"
            if proc.returncode != 0:
                self.last_status = "error"
            return ((proc.stdout or "") + (proc.stderr or "")).strip() or "(no output)"
        if name == "write_file":
            path_arg = str(args.get("path", "")).strip()
            content = args.get("content")
            if not path_arg:
                self.last_status = "error"
                return "error: write_file requires a non-empty 'path' argument."
            if content is None or content == "":
                self.last_status = "error"
                return "error: write_file requires a non-empty 'content' argument."
            p = self._resolve(path_arg)
            p.parent.mkdir(parents=True, exist_ok=True)
            content = str(content)
            p.write_text(content, encoding="utf-8")
            return f"wrote {len(content)} chars to {p}"
        if name == "read_file":
            p = self._resolve(args.get("path", ""))
            if not p.exists():
                self.last_status = "error"
                return f"error: no such file {p}"
            return p.read_text(encoding="utf-8", errors="replace")
        if name == "list_files":
            p = self._resolve(args.get("path", "."))
            if not p.exists():
                return f"error: no such path {p}"
            if p.is_file():
                return str(p)
            return "\n".join(sorted(x.name for x in p.iterdir()))
        return f"[unsupported tool: {name}]"

    def read_answer(self, filename: str) -> str | None:
        p = self.work_root / "answers" / filename
        return p.read_text(encoding="utf-8") if p.exists() else None

    def clear_answer(self, filename: str) -> None:
        p = self.work_root / "answers" / filename
        if p.exists():
            p.unlink()
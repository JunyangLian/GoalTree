"""Workspace-local file identities for receipts; never an authorization bypass."""
import os


def source_path(agent, raw):
    """Reuse the host resolver, then collapse spelling aliases for one file.

    Unresolvable/rejected inputs remain unchanged for failure diagnostics. This
    does not turn a rejected path into an allowed path or execute a tool.
    """
    if not isinstance(raw, str):
        return raw
    try:
        root = getattr(agent, 'root', None) or agent.path('.')
        relative = agent.path(raw).relative_to(root).as_posix()
        return os.path.normcase(relative).replace('\\', '/')
    except (ValueError, OSError):
        return raw

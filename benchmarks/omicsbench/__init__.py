"""Minimal OmicsBench adapter for GoalTree.

This package mirrors the *official* OmicsBench runner protocol (prompt
rendering, DAG traversal, answer-file contract, attempt outcomes) without
importing or re-implementing its graders. Scores are produced only by the
official grader, which runs outside the agent execution path.
"""
---
pretty_name: CodeTrail ProcessBench v0.1 Draft
language:
- en
- zh
- code
license: mit
task_categories:
- text-generation
tags:
- code
- agents
- evaluation
- synthetic
configs:
- config_name: default
  data_files:
  - split: dev
    path: data/dev.jsonl
  - split: test
    path: data/test.jsonl
---
# CodeTrail ProcessBench v0.1 — Draft

This is a **local unpublished draft**, not a validated leaderboard or an online
evaluation service. It contains 12 new small synthetic scenarios, 6 dev and 6
test; all are controller-authored, not real upstream faults. Four repair types,
memory reuse opportunities and stale-memory transitions are covered in each
split. Test labels are public; no model-pretraining or permanent unseen claim.

## Intended use and limitations

Compare coding harnesses with pinned models, budgets and permissions. Task,
repeat, scenario and session counts must be reported separately. Multi-session
scenarios preserve only their own memory. Final test passing alone does not
prove memory use. Required-evidence labels are proxies awaiting independent
human review; alternate correct implementations may use different evidence.
Small examples do not establish whole-repository repair competence.

## Loading

`load_dataset("json", data_files={"dev":"data/dev.jsonl", "test":"data/test.jsonl"})`

Only `files` and `prompt` for the current phase may be installed/sent to agents.
Never paste an entire dataset row into the model: metadata and acceptance scope
belong to the controller. `annotations/` is evaluator-only. Reference repairs
are excluded from this export. Before model runs, pin this export and all
controller, environment, model, permissions and metric revisions separately.

## Validation and results

`qualification.json` is OS-isolated faulty/reference asset self-check evidence,
not Agent success or a memory-effect score. No paid model run is included.
No Agent ranking, total score or synthetic missing metric is published here.
The package is blocked from automatic publication while human gold review and
at least two-Agent controlled evaluation are pending.

## License and provenance

These new synthetic fixture files, tests and annotations are released under
MIT (see LICENSE). No SWE-bench or third-party repository payload is bundled.
Public benchmark sources retain their own licensing and are a separate track.

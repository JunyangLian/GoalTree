# benchmarks/omicsbench

GoalTree 到 OmicsBench 的薄适配层 + 科学约束管道实验入口。

## 状态边界

- 本目录**不在**本次运行中执行官方 Runner 本体：官方 Runner 需 Python ≥ 3.13、Docker、Harbor/Daytona 沙箱，本机为 Python 3.10.1、无 Docker。
- 这里的 `prompt_renderer` 忠诚复刻官方 `prompt/` 渲染（shape-only schema、choice/group-label 选项，不泄露 ground truth 值），`adapter` 复刻官方 DAG 逐轮驱动与答案文件契约。
- **不评分**：答案只做结构分类（`file_missing`/`invalid_json`/`schema_mismatch`/`valid`），真实分数只由外部官方 grader 给出，缺分数记录为 `null`，不填零。

## 文件

- `question.py` — 官方 question JSON 的最小 dataclass 镜像 + 拓扑排序。
- `prompt_renderer.py` — 官方 `system/data/node` 提示渲染（无 Jinja2）。
- `env.py` — 工具执行边界；`MockEnvironment` 仅用于离线测试。
- `adapter.py` — `OmicsBenchAdapter`：逐轮 drive + 答案结构分类。
- `config.py` — 消融组 A/B/C/D 冻结配置。
- `run.py` — 离线规划入口（渲染/校验，不调用模型）。
- `summarize.py` — 归档 manifest/prompts/submissions/summary。

## 使用

```powershell
python -X utf8 -m benchmarks.omicsbench.run --question <官方 question.json> --group A
```

## 测试

```powershell
python -X utf8 -m pytest tests/test_omicsbench_adapter.py tests/test_omicsbench_scientific.py -q
```

全部离线（`FakeModelClient` + `MockEnvironment`），不计 benchmark 分数。
# 合并来源与冻结基线

| 项 | 值 |
|---|---|
| 来源仓库 | `dundundun0116/compassrag`（本地 `/Users/szx/compassrag`） |
| 合并时 HEAD | `cd20f023b59559284f0bf137aa21c94f59aa7c54`（`docs: 项目全貌与状态文档（供外部审查）`） |
| 未提交工作树 | 已按工作树原样迁入；`git diff` 指纹 `8eb7c5ac99ddcc34712a27b2cdef9dd320027933` |
| 合并日期 | 2026-10-30 |
| 仓库归档 | 原 `dundundun0116/compassrag` 已于 2026-10-30 在 GitHub 归档（只读） |
| 历史位置 | 完整 Git 历史在 atlas 的 `compassrag-history` 分支；tag `compassrag-archive-20261030` |
| 迁入范围 | `compassrag/` 包、`tests/`、`configs/`、`data/samples/`、`web/`、`scripts/`、公开文档 |
| 未迁入（留档于 `docs/internal/compassrag/`，不入公开树） | `LEDGER.md`、`PROJECT.md`、`DECISIONS.md`、`JOURNAL.md`、`GOALS.md`、`LESSONS.md`、`decisions/`、`lessons/`、`goals/`、`_stash/`、`_handoff/`、`docs/superpowers/`、`docs/interview-qa.md`、`docs/PROJECT-STATE.md`、`docs/STATUS.md`、两份 2026-10-28 计划 |

合并方式为「单仓双层、边界不变」：CompassRAG 仍是独立包与独立测试基线，只通过 `ResearchTool`
（`atlas/tools/compassrag.py`）与 Atlas 交互；原仓库保留为归档。

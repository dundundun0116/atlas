# 评测口径与方法（CompassRAG）

> 一句话：**同一题集、同一语料索引、同一预算、多次重复**，五组配置各跑一遍；
> 指标分「质量 / 证据 / 成本 / 可靠性」四列，每个汇总数字都能回到逐题 trace。

## 固定条件（每次运行都写进 manifest）

| 项 | 值 |
|---|---|
| 数据集 | MuSiQue dev 分层抽样 300 题清单 → 取前 50 题（`data/samples/musique_dev_n300_seed42.jsonl`） |
| 语料 | `musique__samples`（4,646 块，抽样题 context 池并集） |
| 索引 | BM25 + BGE-M3 稠密 + 实体图（`data/cache/`，指纹写在 manifest） |
| 模型 | `deepseek-v4.1-flash`（OpenAI 兼容端点；名称与版本进 manifest） |
| 预算 | 每题：`top_k=10`、`max_evidence=20`、决策类 `max_rounds=3`；答案 ≤5 词约束（见下） |
| 重复 | 每个配置 ≥2 次；报告均值 ± 标准差 |
| 代码版本 | git commit 写进 manifest（`git_commit` / `git_dirty`） |

**答案格式约束**：`strict_answer_words=5`。这是 `decisions/D11` 的实验结论——不加约束时严格 EM
会被"答得太完整"压低约 0.14（`661` 写成 `661 CE`、`June 10, 1819` 写成 `10 June 1819`）。
正式评测统一加约束，并在报告里注明；与历史无约束数字比较时必须说明口径不同。

## 五组配置

| 配置 | 含义 | 对照问题 |
|---|---|---|
| `naive` | 单轮固定检索 + 直接生成 | 基线 |
| `plus_rewrite` | + 多跳分解与 HyDE 改写 | 查询侧是否提升作答质量 |
| `plus_kg` | + 实体关系图 PPR 第三路 | 索引侧是否提升证据覆盖 |
| `plus_decision` | + 路由 / 多跳 / 充分度早停 | 自适应检索是否值得多花 token |
| `plus_verify` | + claim 核验与有限修复 | 证据闭环买到的是可信度还是准确率 |

## 指标（四列）

- **质量**：严格 EM（SQuAD 口径，对外主指标）、宽松 EM（诊断口径，`decisions/D10`）、F1
- **证据**：证据召回@10、full-hit@10、claim support rate
- **成本**：token/题、LLM 调用/题、检索轮数/题、EM per 1k token
- **可靠性**：拒答率、工具失败率、解析降级率、早停率、修复成功率、停止原因分布

## 复现

```bash
# 单配置（调试）
python scripts/eval_qa.py --benchmarks musique --variant samples \
    --config configs/ablation/plus_verify.yaml --limit 50 --strict-answer-words 5

# 正式评测（N 次重复、可并行）
python scripts/run_resume_eval.py --repeats 2 --parallel 3 --strict-answer-words 5
# → runs/eval/r1, r2（各含 jsonl / traces / manifests）+ runs/eval/summary.json

# 汇总表 / 双臂判定
python scripts/report_runs.py
python scripts/compare_arms.py --base plus_full --arm plus_full_words
```

## 口径纪律（写给自己）

1. **严格 EM 是对外口径**；宽松 EM 只作诊断，不进简历主数字。
2. 汇总表里每个数字必须能回到 `runs/eval/r{n}/traces/` 的逐题 trace（`replay_rows` 可离线重算）。
3. 不同样本量、不同索引指纹、不同措辞口径的行**不得直接比较**；`report_runs.py` 会对样本量差异报警。
4. token/题随模型行为漂移（重试率变化），跨时间比较只看 EM/F1 与证据指标。
5. 只有 `summary.json` 生成、且与 trace 对得上，才允许把数字写进简历。

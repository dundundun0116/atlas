# 元数据核验表（Lead 亲自核验，2026-11-04）

> 用途：本目录所有分报告（rq1–rq4）引用论文时，**venue 一栏以本表为准**；本表没有的，写「arXiv preprint（会议归属待核实）」。
> 核验渠道（实测可用性）：OpenReview API v2 `notes/search` 的 `content.venue.value`（NeurIPS/ICLR/TMLR/COLM 最权威）；arXiv abs 页 `Comments:` 字段（作者自填，curl 批量可抓）；PMLR 卷 TOC grep（ICML/CoRL）；Crossref `?mailto=`（AAAI/ACM 等有 DOI 的）；ACL Anthology。
> 不可用渠道：api.openalex.org（429 预算耗尽）、api.semanticscholar.org（429）、dblp.org（Anubis 反爬 202）、proceedings.neurips.cc / papers.nips.cc（本机不可达）、OpenReview `/notes?content.title=`（需过 challenge）。

## 已核实

| 简称 | 标题 | 年份 | venue（原样） | 链接 | 核验方式 |
|---|---|---|---|---|---|
| ReAct | ReAct: Synergizing Reasoning and Acting in Language Models | 2022/2023 | ICLR 2023 | https://arxiv.org/abs/2210.03629 | arXiv Comments：「v3 is the ICLR camera ready version」 |
| Plan-and-Solve | Plan-and-Solve Prompting: Improving Zero-Shot Chain-of-Thought Reasoning by Large Language Models | 2023 | ACL 2023 | https://arxiv.org/abs/2305.04091 | arXiv Comments：「ACL 2023」 |
| ToT | Tree of Thoughts: Deliberate Problem Solving with Large Language Models | 2023 | NeurIPS 2023（oral） | https://arxiv.org/abs/2305.10601 | OpenReview venue=「NeurIPS 2023 oral」+ arXiv Comments「NeurIPS 2023 camera ready」 |
| LATS | Language Agent Tree Search Unifies Reasoning, Acting, and Planning in Language Models | 2023/2024 | ICML 2024 | https://arxiv.org/abs/2310.04406 | PMLR v235（ICML 2024）TOC 命中；注意 OpenReview 只有「Submitted to ICLR 2024」（=投稿记录，不是录用） |
| Reflexion | Reflexion: Language Agents with Verbal Reinforcement Learning | 2023 | NeurIPS 2023（poster） | https://arxiv.org/abs/2303.11366 | OpenReview venue=「NeurIPS 2023 poster」 |
| SayCan | Do As I Can, Not As I Say: Grounding Language in Robotic Affordances | 2022 | CoRL 2022 | https://arxiv.org/abs/2204.01691 | PMLR v205（CoRL 2022）TOC 命中 |
| Voyager | Voyager: An Open-Ended Embodied Agent with Large Language Models | 2023 | TMLR（2024 接收） | https://arxiv.org/abs/2305.16291 | OpenReview venue=「Accepted by TMLR」 |
| SWE-agent | SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering | 2024 | NeurIPS 2024（poster） | https://arxiv.org/abs/2405.15793 | OpenReview venue=「NeurIPS 2024 poster」 |
| AutoGen | AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation | 2023 | COLM 2024 | https://arxiv.org/abs/2308.08155 | OpenReview venue=「COLM」（另有一条「Submitted to ICLR 2024」=未录用记录） |
| LLMCompiler | An LLM Compiler for Parallel Function Calling | 2023/2024 | ICML 2024 | https://arxiv.org/abs/2312.04511 | arXiv Comments：「ICML 2024」 |
| ADaPT | ADaPT: As-Needed Decomposition and Planning with Language Models | 2023/2024 | NAACL 2024 Findings | https://arxiv.org/abs/2311.05772 | arXiv Comments：「NAACL 2024 (findings) camera-ready」 |
| HuggingGPT | HuggingGPT: Solving AI Tasks with ChatGPT and its Friends in Hugging Face | 2023 | NeurIPS 2023（poster） | https://arxiv.org/abs/2303.17580 | OpenReview venue=「NeurIPS 2023 poster」 |
| Generative Agents | Generative Agents: Interactive Simulacra of Human Behavior | 2023 | UIST 2023 | https://arxiv.org/abs/2304.03442 | **Crossref DOI 10.1145/3586183.3606763**：container-title=「Proceedings of the 36th Annual ACM Symposium on User Interface Software and Technology」，type=proceedings-article，2023-10-29 |
| MemGPT | MemGPT: Towards LLMs as Operating Systems | 2023 | arXiv preprint（CoRR） | https://arxiv.org/abs/2310.08560 | OpenReview venue=「CoRR 2023」；Comments 只给代码链接 |
| Mem0 | Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory | 2025 | arXiv preprint | https://arxiv.org/abs/2504.19413 | arXiv Comments 无会议信息 |
| MemoryBank | MemoryBank: Enhancing Large Language Models with Long-Term Memory | 2023/2024 | AAAI 2024 | https://arxiv.org/abs/2305.10250 | Crossref：container-title=Proceedings of the AAAI Conference on Artificial Intelligence，2024-03-24 |
| HippoRAG | HippoRAG: Neurobiologically Inspired Long-Term Memory for Large Language Models | 2024 | NeurIPS 2024 | https://arxiv.org/abs/2405.14831 | arXiv Comments：「NeurIPS 2024」 |
| GraphRAG | From Local to Global: A Graph RAG Approach to Query-Focused Summarization | 2024 | arXiv preprint | https://arxiv.org/abs/2404.16130 | arXiv Comments 无会议信息 |
| LongMemEval | LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory | 2024/2025 | ICLR 2025 | https://arxiv.org/abs/2410.10813 | arXiv Comments：「ICLR 2025」 |
| A-MEM | A-MEM: Agentic Memory for LLM Agents | 2025 | NeurIPS 2025 | https://arxiv.org/abs/2502.12110 | arXiv Comments：「Advances in Neural Information Processing Systems (NeurIPS 2025)」 |
| MemOS | MemOS: A Memory OS for AI System | 2025 | arXiv preprint | https://arxiv.org/abs/2507.03724 | arXiv Comments 无会议信息 |
| MemoryOS | Memory OS of AI Agent | 2025 | EMNLP 2025（main） | https://arxiv.org/abs/2506.06326 | ACL Anthology 页 `aclanthology.org/2025.emnlp-main.1318/` 存在且标题一致（rq2 另用 bibtex 核到 pp.25961–25970）；arXiv Comments 无会议信息 |
| CoALA | Cognitive Architectures for Language Agents | 2023/2024 | TMLR | https://arxiv.org/abs/2309.02427 | arXiv Comments：「v3 is TMLR camera ready」+ OpenReview「Accepted by TMLR」 |
| Self-RAG | Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection | 2023/2024 | ICLR 2024（oral） | https://arxiv.org/abs/2310.11511 | OpenReview venue=「ICLR 2024 oral」 |
| WebArena | WebArena: A Realistic Web Environment for Building Autonomous Agents | 2023/2024 | ICLR 2024（poster） | https://arxiv.org/abs/2307.13854 | OpenReview notes/search 命中「ICLR 2024 poster」 |
| BrowserGym | The BrowserGym Ecosystem for Web Agent Research | 2024/2025 | TMLR 2025 | https://arxiv.org/abs/2412.05467 | OpenReview venue=「Trans. Mach. Learn. Res. 2025」 |
| OSWorld | OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks in Real Computer Environments | 2024 | NeurIPS 2024 | https://arxiv.org/abs/2404.07972 | OpenReview venue=「NeurIPS 2024」 |
| τ-bench | τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains | 2024/2025 | ICLR 2025（poster） | https://arxiv.org/abs/2406.12045 | OpenReview venue=「ICLR 2025 Poster」 |
| AgentBench | AgentBench: Evaluating LLMs as Agents | 2023/2024 | ICLR 2024（poster） | https://arxiv.org/abs/2308.03688 | arXiv Comments：「Published in ICLR 2024」+ OpenReview「ICLR 2024 poster」 |
| SWE-bench | SWE-bench: Can Language Models Resolve Real-World GitHub Issues? | 2023/2024 | ICLR 2024 | https://arxiv.org/abs/2310.06770 | arXiv Comments：「ICLR 2024」 |
| Toolformer | Toolformer: Language Models Can Teach Themselves to Use Tools | 2023 | NeurIPS 2023（oral） | https://arxiv.org/abs/2302.04761 | OpenReview venue=「NeurIPS 2023 oral」 |
| AppWorld | AppWorld: A Controllable World of Apps and People for Benchmarking Interactive Coding Agents | 2024 | ACL 2024 | https://arxiv.org/abs/2407.18901 | arXiv Comments：「ACL'24 Camera Ready」 |
| TheAgentCompany | TheAgentCompany: Benchmarking LLM Agents on Consequential Real World Tasks | 2024 | arXiv preprint | https://arxiv.org/abs/2412.14161 | arXiv Comments：「Preprint」 |
| AgentBoard | AgentBoard: An Analytical Evaluation Board of Multi-turn LLM Agents | 2024 | NeurIPS 2024（Oral） | https://arxiv.org/abs/2401.13178 | arXiv Comments：「NeurIPS 2024 (Oral)」 |
| GAIA | GAIA: a benchmark for General AI Assistants | 2023/2024 | ICLR 2024（poster） | https://arxiv.org/abs/2311.12983 | OpenReview venue=「ICLR 2024 poster」 |
| TRAIL | TRAIL: Trace Reasoning and Agentic Issue Localization | 2025 | arXiv preprint | https://arxiv.org/abs/2505.08638 | arXiv Comments 只给 dataset 链接 |
| ReWOO | ReWOO: Decoupling Reasoning from Observations for Efficient Augmented Language Models | 2023 | arXiv preprint | https://arxiv.org/abs/2305.18323 | arXiv Comments 无会议信息 |

## 需要额外注意的坑（已踩）

1. **OpenReview 的「Submitted to XXX」不是录用**。LATS、AutoGen 都出现过 ICLR 投稿记录，实际发表渠道分别是 ICML 2024 与 COLM 2024。
2. **paper_cite 返回的是 DOI 注册库的 arXiv 版本**，venue 一律显示 arXiv，不能用来证明会议。
3. **paper_search 的 venue 列也只显示 arXiv**（索引层口径），同理。
4. NeurIPS 论文的权威页在本机不可达，改用 OpenReview venue 或 arXiv Comments 交叉验证。
5. 同一工作常有「arXiv 年份 ≠ 会议年份」，本表年份同时给出（如 2023/2024）。

## 追加核验（2026-11-04 晚些时候，来自 rq2 与 Lead 复核）

| 简称 | 标题（实取） | 年份 | venue | 链接 | 核验方式 |
|---|---|---|---|---|---|
| Lost in the Middle | Lost in the Middle: How Language Models Use Long Contexts | 2024 | TACL 2024 | https://doi.org/10.1162/tacl_a_00638 | Crossref DOI：container-title=Transactions of the Association for Computational Linguistics，type=journal-article |
| Horizon Gap | The Horizon Gap: Planning, Memory, Execution, Training, and Evaluation | 2026 | arXiv preprint（待核实） | https://arxiv.org/abs/2608.06663 | arXiv abs 标题实取；Comments 只有「39 pages, 6 figures」 |
| Ground Truth First | Ground Truth First: A Longitudinal Evaluation Instrument for Agent Mem…（标题在 abs 页被截断） | 2026 | arXiv preprint（待核实） | https://arxiv.org/abs/2607.21962 | arXiv abs 标题实取；Comments 只有「25 pages…Code」 |
| Memory-R1 | Memory-R1: Enhancing Large Language Model Agents to Manage and Utilize…（标题在 abs 页被截断） | 2025 | arXiv preprint（待核实） | https://arxiv.org/abs/2508.19828 | arXiv abs 标题实取；Comments 无会议信息 |
| MemOS（另一篇） | MemOS: An Operating System for Memory-Augmented Generation (MAG) in La… | 2025 | arXiv preprint（待核实） | https://arxiv.org/abs/2505.22101 | **注意**：与 2507.03724「MemOS: A Memory OS for AI System」是**两篇不同论文**，引用时必须写清是哪一篇 |

> 追加教训：2025–2026 的 arXiv 记录里，同一项目常有 2–3 篇标题相近的论文（MemOS 两篇、Memory OS/MemoryOS 混用）。**引用前必须核 abs 页标题原文**，不能凭项目名缩写。

## 追加核验（来自 rq3，Lead 复核过的部分）

| 简称 | venue | 链接 | 核验方式 |
|---|---|---|---|
| OpenHands | ICLR 2025（Poster） | https://arxiv.org/abs/2407.16741 | OpenReview `notes/search` 命中「ICLR 2025 Poster」 |
| WorkArena | ICML 2024（Poster） | https://arxiv.org/abs/2403.07718 | OpenReview 命中「ICML 2024 Poster」；另有「LLMAgents @ ICLR 2024 Poster」为 workshop 记录，勿混用 |
| TheAgentCompany | NeurIPS 2025 Datasets and Benchmarks Track（poster） | https://arxiv.org/abs/2412.14161 | OpenReview 命中「NeurIPS 2025 Datasets and Benchmarks Track poster」。**注意**：arXiv Comments 仍写「Preprint」，是陈旧自述；以 OpenReview 为准，并在正文注明"arXiv 自述为 Preprint，OpenReview 显示已录用" |

**关于 τ-bench 的争议（保留判断）**：rq3 报告称 OpenReview 未命中、官方 README BibTeX 为 `@misc{yao2024tau}`；Lead 用 `notes/search` 以完整标题检索时**命中「ICLR 2025 Poster」**（term="tau-bench"）。两者不矛盾——README 的 BibTeX 常滞后于录用。**本报告采信 ICLR 2025 poster**，并保留此条分歧记录以备复查。

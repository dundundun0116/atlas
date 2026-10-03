# Atlas 项目工作约定

## 每次工作的收尾（强制，无需征求同意）

1. 跑测试：`.venv/bin/pytest tests/`，全部通过才算完成；
2. git 提交并推送：一个工作单元一条提交，提交信息写清做了什么；
3. 更新 PROJECT.md：
   - 第 11 节"进度日志"加条目（最新在最上，一行式：日期 + 做了什么 + 产出/结论/卡点）；
   - 第 8 节里程碑表状态同步；
   - 设计决策变更必须先经用户拍板（见第 9/10 节格式），不得擅改。

## 环境速查

- 项目根 venv：`.venv`（pytest / tiktoken）；包路径解析由 pyproject.toml 的 `pythonpath=["."]` 提供
- 评测 venv：`external/MemoryArena/.venv`（uv 创建，Python 3.12）；服务启动与评测跑法见 `eval/README.md`
- API 凭据：`eval/.env`（已 gitignore）；换后端只改该文件 + `eval/configs/`
- macOS 网络走系统代理（Clash，127.0.0.1:7897）时按需 `export https_proxy`

## 仓库结构

- `PROJECT.md`：定位 / 决策 / 里程碑 / 进度日志；`SURVEY.md`：调研；`notes/`：论文精读
- `atlas/`：核心包（`memory/` 图引擎；`context/` 组装与结转；`agents/`、评测接入层后续添加）
- `tests/`：单元测试（TDD：先写测试看它失败，再实现）
- `eval/`：评测脚手架（lite memory server、configs）
- `external/`：第三方仓库克隆（gitignored）；`runs/`：评测产物（gitignored）

## 协作方式

- 方案讨论时给选项+取舍，用户拍板后才落盘，不擅自定稿；
- 密钥一律不入库；
- 论文配图等约定见用户全局 AGENTS.md。

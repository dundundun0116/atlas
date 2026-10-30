"""跨层集成物：Atlas 运行时 × CompassRAG 检索层的任务级评测/演练。

放在两个包之外是刻意的：`atlas/` 与 `compassrag/` 之间只经 `ResearchTool` 交互，
跨层脚手架不该被塞进任一方的包，也不随 `pip install` 分发。
"""

"""对外适配层（阶段 E）：Atlas 通过 ResearchTool 协议调用 CompassRAG。"""

from .atlas import HTTPResearchTool, LocalResearchTool, ResearchTool

__all__ = ["ResearchTool", "LocalResearchTool", "HTTPResearchTool"]

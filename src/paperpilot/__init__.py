"""PaperPilot — arXiv 每日文献情报系统。

分层：
- domain  纯业务（实体 DTO、AI 契约 Port、策略、流水线编排），不认识 HTTP / SQLite
- infra   外部世界（arXiv API、SQLite、AI 实现、渲染、通知）
- app     Web UI / CLI / 调度 / DI
"""

__version__ = "0.1.0"

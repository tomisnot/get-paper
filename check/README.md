# 验证的运行方式

```powershell
.venv\Scripts\python -m pytest                    # 默认离线契约与结果
.venv\Scripts\python -m pytest -m integration     # 真实 HTTP/MCP 与子进程
.venv\Scripts\python -m pytest -m ""              # 全部离线验证
.venv\Scripts\python -m pytest check/test_card_single_source.py # 只跑受影响场景
```

check/ 与 tests/ 的验证地位相同。结构测试保护参数投影、人类专属能力、原始来源等关系；结果测试验证筛选、摘要、简报和治理链路。共享框架机制由框架验证，项目验证自己的实际接线。

退役 test_skill_sync.py：自由文本中的反引号 token 不能可靠代表工具调用，不要求每个工具逐名写入技能文档。能力、工具总数不再钉死；投影一致性集中在 test_capability_map，仍禁止将人类专属能力投影给 AI。

技能说明工作流与知识；完整工具与参数由宿主 schema 提供。文档不成为测试的第二份注册表，不要求固定章节、计数或每条配红证。历史设计日志中的门禁与运行数量仅描述当时状态。

涉及 dsh 接线时在 dsh 运行 npm test（TypeScript + 现有 platform=browser 真实构建）。共享资产行为由 mecha 验证，不另维护源码 import 解析器、固定 bootstrap 拼写或文件黑名单。

全量还发现旧 CLI 私有端口函数的测试：实现已上提至 mecha.portfile，旧函数不存在。删除这些重复守卫，框架端口契约与本项目真实服务启动测试保留。

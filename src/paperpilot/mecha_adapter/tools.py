"""PaperPilot 的 AI 工具面：把 22 个中性能力包装成 mecha ``define_tool``。

三条纪律（接入指南第 2/3 步 + 交接文档 §7/§8）：

1. **单一来源**：领域实现仍住 ``capabilities/``——工具的 ``execute`` 只是
   ``capabilities.invoke`` 的薄桥；参数声明从能力的自描述 ``params`` **机械派生**
   （不手抄第二份参数表，消 D4）。信封失败（``ok=False``）转成 ``MechaError``
   抛出，让 ``ToolRegistry`` 归一化。
2. **命名改造**：mecha 禁 ``get_``/``list_`` 前缀、要 snake_case 动词_宾语**至少两段**
   （``_NAME_RE``）。据此 ``get_*→read_*``、``list_topics→query_topics``。
   ⚠ ``undo`` 是**单段**、不合命名律（交接文档 §7 误判为"已合规"）⇒ 改 ``undo_change``
   ——记进 n=3 报告。
3. **R5 三分法**：``defaulted`` 的唯一权威来源是声明里的 ``default`` 键。凡能力的
   真默认**非 None** 的可选参数（本项目全部如此：``""``/``True``/``0``/…）都在声明里
   写出 ``default``；必填参数不写。``execute`` 用 ``**kwargs`` 包装 ⇒ 签名推必填得空集，
   故另由 ``PaperPilotRequiredSource`` 注入权威必填项（MCP 投影消费）。

写工具的 ``actor`` **不作为模型可见参数**：由 ``channel.actor`` 钉死注入（AI 侧恒 'ai'，
改不了）——与 mecha「通道钉死 actor」一致。``read_activity`` 的 ``actor`` 是**过滤条件**
（不是归因），故保留。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from mecha.errors import MechaError
from mecha.gate import Channel
from mecha.tools import CURRENT_CALL_ID, ToolRegistry, define_tool

from .. import capabilities
from .engine import CONFIG_SCHEMA

#: description 只准含任务概念，禁实现词汇（define_tool 的 banned_words 守卫）。
_BANNED_WORDS = (
    "fts5", "mcp", "sqlalchemy", "sqlite", "settings.yaml",
    "append-only", "yaml", "journal", "container", "repo",
)


@dataclass(frozen=True)
class ToolDecl:
    """一条工具声明（mecha 名 → 能力名 + 领域文案 + 参数处置）。"""

    mecha_name: str
    cap_name: str
    kind: str                       # "read" | "write"
    description: str
    via_surface: bool = False        # True = 走 Engine.run（每日流水线）
    omit: tuple[str, ...] = ()       # 不作为模型可见参数的能力入参
    param_desc: dict = field(default_factory=dict)


#: 22 能力的投影表（单一来源：参数派生、必填注入、Surface 注册都由它驱动）。
TOOL_DECLS: tuple[ToolDecl, ...] = (
    # ------------------------------------------------------------ 只读面（9）
    ToolDecl("query_topics", "list_topics", "read",
             "列出研究主题：名称、关键词、分类白名单、配额、评分阈值、启用状态。"),
    ToolDecl("read_digest", "get_digest", "read",
             "读取某日简报全文（Markdown）与统计。",
             param_desc={"date": "简报日期 ISO 格式（省略=今天）",
                         "full": "True 含 Markdown 全文；False 只回统计与条目"}),
    ToolDecl("search_papers", "search_papers", "read",
             "在论文库中检索（标题/摘要/要点全文匹配）。query 省略则按时间倒序列出近期论文。",
             param_desc={"query": "检索词（省略=列近期）", "label": "按档位过滤",
                         "category": "按主分类过滤", "limit": "最多返回条数（1-100）"}),
    ToolDecl("read_paper", "get_paper", "read",
             "论文详情：原文摘要、最新 AI 总结、打分历史、笔记、阅读态、本地 PDF 路径。",
             param_desc={"arxiv_id": "论文 arXiv 编号"}),
    ToolDecl("read_activity", "get_activity", "read",
             "归因面：①操作事件流（可按操作者/操作类型/序号过滤）②近期运行、AI 调用与简报统计。",
             param_desc={"since_seq": "只回此序号之后的事件", "actor": "按操作者过滤",
                         "op": "按操作类型过滤", "days": "背景统计的回溯天数",
                         "limit": "事件最多返回条数"}),
    ToolDecl("review_status", "review_status", "read",
             "查看某天评审进度：候选数、已评审数、状态。",
             param_desc={"date": "日期 ISO 格式（省略=今天）"}),
    ToolDecl("paper_metrics", "paper_metrics", "read",
             "一篇论文的影响力指标：被引数、参考数、高影响引用数、年份、发表场所、要点摘要。"
             "用于判断分量与质量信号。",
             param_desc={"arxiv_id": "论文 arXiv 编号"}),
    ToolDecl("read_references", "get_references", "read",
             "取一篇论文引用的文献（往前追溯技术起源）。默认按被引论文引用数降序——排最前的"
             "即起源/奠基候选；含引用意图与是否高影响引用。可对结果递归再查以继续往前追溯。",
             param_desc={"arxiv_id": "论文 arXiv 编号", "limit": "最多返回条数（≤100）",
                         "sort_by_citations": "是否按引用数降序"}),
    ToolDecl("read_citations", "get_citations", "read",
             "取引用了这篇论文的文献（往后看影响力扩散与后续工作）。可按引用数降序。",
             param_desc={"arxiv_id": "论文 arXiv 编号", "limit": "最多返回条数（≤100）",
                         "sort_by_citations": "是否按引用数降序"}),
    # ------------------------------------------------------------ 写入 / 运行面（13）
    ToolDecl("undo_change", "undo", "write",
             "撤销一条可逆的写入（seq=0=最近一条可逆操作）。入库与定稿不可逆，会明确说明。",
             omit=("actor",),
             param_desc={"seq": "要撤销的事件序号（0=最近一条可逆）",
                         "reason": "一句话中文说明撤销原因"}),
    ToolDecl("fetch_papers", "fetch_papers", "write",
             "按主题抓取 arXiv 最近 N 天提交的新论文入库（遵守 arXiv 限速，可能较慢）。",
             omit=("actor",),
             param_desc={"days": "回溯天数", "reason": "一句话中文说明本次抓取目的"}),
    ToolDecl("download_paper", "download_paper", "write",
             "下载论文 PDF 到本地库并归档（幂等：已下载直接返回本地路径）。",
             omit=("actor",),
             param_desc={"arxiv_id": "论文 arXiv 编号", "reason": "一句话中文说明下载原因"}),
    ToolDecl("prepare_review", "prepare_review", "write",
             "评审阶段1：取过规则后的候选清单（含主题画像、摘要截断、基线分），等待评审。",
             omit=("actor",),
             param_desc={"date": "日期 ISO 格式（省略=今天）", "reason": "一句话中文说明目的"}),
    ToolDecl("submit_review", "submit_review", "write",
             "评审阶段2：提交对候选的评审。reviews=[{arxiv_id,score,label,reason,tags?,summary?}]。",
             omit=("actor",),
             param_desc={"date": "评审对应日期 ISO 格式", "reviews": "评审列表",
                         "reason": "一句话中文说明目的"}),
    ToolDecl("finalize_briefing", "finalize_briefing", "write",
             "评审阶段3：用已提交评审（缺的用基线分）筛选、精读、生成简报并落库。",
             omit=("actor",),
             param_desc={"date": "日期 ISO 格式（省略=今天）", "force": "已定稿时是否强制重跑",
                         "reason": "一句话中文说明目的"}),
    ToolDecl("run_pipeline", "run_pipeline", "write",
             "一键全流程（无外部评审）：候选→规则→程序化打分（未配模型时用启发式兜底）→简报。",
             via_surface=True, omit=("actor",),
             param_desc={"date": "日期 ISO 格式（省略=今天）", "force": "已存在时是否强制重跑",
                         "reason": "一句话中文说明目的"}),
    ToolDecl("add_topic", "add_topic", "write",
             "新增研究主题（即时生效）。keywords/categories/exclude_keywords 用逗号分隔。",
             omit=("actor",),
             param_desc={"name": "主题名", "keywords": "关键词，逗号分隔",
                         "categories": "arXiv 分类白名单，逗号分隔", "description": "主题描述",
                         "exclude_keywords": "排除词，逗号分隔", "quota": "每主题配额",
                         "threshold": "入选评分阈值", "reason": "一句话中文说明新增原因"}),
    ToolDecl("set_topic_enabled", "set_topic_enabled", "write",
             "启用或停用某个研究主题。",
             omit=("actor",),
             param_desc={"name": "主题名", "enabled": "True 启用 / False 停用",
                         "reason": "一句话中文说明原因"}),
    ToolDecl("mark_read", "mark_read", "write",
             "标记论文为已读或未读。",
             omit=("actor",),
             param_desc={"arxiv_id": "论文 arXiv 编号", "read": "True 已读 / False 未读",
                         "reason": "一句话中文说明原因"}),
    ToolDecl("star_paper", "star_paper", "write",
             "收藏或取消收藏论文。",
             omit=("actor",),
             param_desc={"arxiv_id": "论文 arXiv 编号", "reason": "一句话中文说明原因"}),
    ToolDecl("skip_paper", "skip_paper", "write",
             "标记论文为不感兴趣（同类下次过滤）。",
             omit=("actor",),
             param_desc={"arxiv_id": "论文 arXiv 编号", "reason": "一句话中文说明原因"}),
    ToolDecl("add_note", "add_note", "write",
             "给论文添加笔记（调研沉淀）。",
             omit=("actor",),
             param_desc={"arxiv_id": "论文 arXiv 编号", "content": "笔记正文",
                         "reason": "一句话中文说明原因"}),
)

#: mecha 名 → 能力名（判据/宿主自省用的单一来源投影）。
TOOL_TO_CAPABILITY: dict[str, str] = {d.mecha_name: d.cap_name for d in TOOL_DECLS}


def _suggest_text(suggest: object) -> str:
    """能力信封的 suggest（可能是 list）→ MechaError.suggest（str）。"""
    if isinstance(suggest, (list, tuple)):
        return "、".join(str(x) for x in suggest)
    return str(suggest or "")


def _cap_params(container, cap_name: str) -> dict:
    spec = capabilities.registry_for(container).get(cap_name)
    if spec is None:                       # 声明表与能力层脱节：fail loud（不静默漏工具）
        raise MechaError(f"能力 {cap_name!r} 不在能力层", kind="capability_missing",
                         hint="工具声明表引用了不存在的能力；核对 capabilities/tools.py")
    return dict(spec.params)


def _derive_parameters(cap_params: Mapping[str, dict], decl: ToolDecl) -> dict:
    """从能力自描述 params 机械派生 mecha 参数声明（R5 三分法）。"""
    out: dict[str, dict] = {}
    for pname, info in cap_params.items():
        if pname in decl.omit:
            continue
        entry: dict[str, object] = {}
        jtype = info.get("type")
        if jtype:
            entry["type"] = jtype
        # 可选项且真默认非 None ⇒ 必须声明 default（框架 null 垫片只读声明）。
        if not info.get("required") and info.get("default") is not None:
            entry["default"] = info["default"]
        if pname in decl.param_desc:
            entry["description"] = decl.param_desc[pname]
        out[pname] = entry
    return out


def _required_names(cap_params: Mapping[str, dict], decl: ToolDecl) -> list[str]:
    return [p for p, i in cap_params.items()
            if i.get("required") and p not in decl.omit]


def _raise_from_envelope(res: Mapping[str, object]) -> None:
    """把能力失败信封转成 MechaError 抛出（ToolRegistry.execute 会归一化）。"""
    e = res.get("error") or {}
    if not isinstance(e, Mapping):
        e = {}
    raise MechaError(
        str(e.get("message") or "调用失败"),
        kind=str(e.get("kind") or "error"),
        hint=str(e.get("hint") or ""),
        suggest=_suggest_text(e.get("suggest")),
    )


def build_tool_registry(container, sw, channel: Channel | None = None) -> ToolRegistry:
    """装配 PaperPilot 的工具面（24 工具 = 22 能力 + set_config/read_config）。

    - **只读能力**：``execute`` 直接桥到 ``capabilities.invoke``（不经门，D-4）。
    - **写入能力**：``execute`` 桥到 ``sw.commands.invoke(gate=sw.gate, channel=ai)``
      ——写权（authority）与审计（``command.<name>`` 落 History）由命令面治理（Phase 2）。
    - **配置标量**：``set_config`` 经 ``gate.set`` 写（authority + validate 双闸），
      ``read_config`` 读快照——配置态归 Gate（Scheme E）。

    ``channel`` 缺省 = ``sw.channels['ai']``（AI 侧，actor 钉死 'ai'）。
    """
    channel = channel or sw.channels["ai"]
    reg = ToolRegistry()
    for decl in TOOL_DECLS:
        cap_params = _cap_params(container, decl.cap_name)
        parameters = _derive_parameters(cap_params, decl)
        if decl.kind == "write":
            execute = _make_command_bridge(sw.commands, sw.gate, channel, decl.mecha_name)
        else:
            execute = _make_capability_bridge(container, decl.cap_name)
        reg.register(define_tool(
            name=decl.mecha_name,
            description=decl.description,
            parameters=parameters,
            output_schema={"type": "object", "required": ["ok"]},
            execute=execute,
            banned_words=_BANNED_WORDS,
        ))
    _register_config_tools(reg, sw.gate, channel)
    return reg


def _make_capability_bridge(container, cap_name: str):
    """只读能力的 execute 桥（不经门；失败信封转 MechaError 抛出）。"""
    def _exec(**kwargs):
        res = capabilities.invoke(container, cap_name, **dict(kwargs))
        if not res.get("ok"):
            _raise_from_envelope(res)
        return res
    return _exec


def _make_command_bridge(commands, gate, channel: Channel, cmd_name: str):
    """写入能力的 execute 桥：经命令面 invoke（authority 写权闸 + Gate 审计）。

    ``call_id`` 从请求作用域读并随 args 传入——命令面把它抽给 Gate 审计事件，
    把这次工具调用与宿主行为史钉在一起（总纲 §5.2② 互引）。经 ``invoke_command``
    统一入口把 ai 通道传给 handler（写权闸/归因用正确 side/actor）。
    """
    from .commands import invoke_command  # 延迟导入：commands 依赖 tools，避免模块级循环

    def _exec(**kwargs):
        args = dict(kwargs)
        args["call_id"] = CURRENT_CALL_ID.get()
        # reason 在命令面是必填（才会透传给 handler、写进域 journal）；对模型仍可选，
        # 故省略时桥接填默认空串（不影响命令 required 契约）。
        args.setdefault("reason", "")
        res = invoke_command(commands, gate, channel, cmd_name, args)
        if res["is_error"]:
            failure = res["error"]
            info = failure.get("info", {})
            raise MechaError(
                str(failure.get("message") or "写入失败"),
                kind=str(info.get("kind") or "error"),
                hint=str(info.get("hint") or ""),
                suggest=str(info.get("suggest") or ""),
            )
        return res["value"]
    return _exec


def _register_config_tools(reg: ToolRegistry, gate, channel: Channel) -> None:
    """配置态工具（Scheme E：标量走 Gate）：set_config 经 gate.set，read_config 读快照。

    set_config 是**命名 lambda**（签名可用）⇒ MCP 投影的 required 从签名派生即对，
    不需 RequiredSource 注入。写经 ``gate.set`` ⇒ authority（写权模式）+ validate
    （键/类型/值域）双闸；LOCKED 时 GateDenied，快照一字节不动。
    """
    reg.register(define_tool(
        name="set_config",
        description=(
            "设置一个标量配置键的值（如评分阈值、抓取回溯天数、每主题配额）。"
            "键必须是可写配置键（见 read_config 的清单）；类型/值域不符会被拒并说明。"
            "改动即时对后续每日流水线生效，并留可追溯记录。"
            "reason 用一句中文写清这次改动的目的。"
        ),
        parameters={"key": {"type": "string",
                            "description": "可写配置键，如 scoring.threshold"},
                    "value": {"description": "新值，类型须匹配该键（见 read_config）"},
                    "reason": {"type": "string", "default": "ai set_config",
                               "description": "一句话中文说明改动目的"}},
        output_schema={"type": "object", "required": ["ok", "key"]},
        execute=lambda key, value, reason="ai set_config": _set_config(
            gate, channel, key, value, reason),
        banned_words=_BANNED_WORDS,
    ))
    reg.register(define_tool(
        name="read_config",
        description=(
            "读取当前生效的标量配置（评分阈值、抓取回溯天数、配额等）与可写键清单"
            "（含类型/值域）。写 set_config 前先看这里确认键名与当前值。"
        ),
        parameters={},
        output_schema={"type": "object", "required": ["ok", "config"]},
        execute=lambda: _read_config(gate),
        banned_words=_BANNED_WORDS,
    ))


def _coerce_config(key: str, value):
    """数值/整型配置键：把数字字符串收编回数值（宿主对未声明 type 的参数会转 str）。"""
    rec = CONFIG_SCHEMA.get(key) or {}
    tname = rec.get("type")
    if isinstance(value, str) and tname in ("int", "float"):
        s = value.strip()
        try:
            return int(s) if tname == "int" else float(s)
        except ValueError:
            return value          # 非纯数字串：交 validate 如实拒（不猜）
    return value


def _set_config(gate, channel: Channel, key: str, value, reason: str):
    before = gate.snapshot.get(key)
    ev = gate.set(channel, key, _coerce_config(key, value), reason=reason,
                  call_id=CURRENT_CALL_ID.get())
    # 回执自带回读：resolved=实际落地值、readback=从快照读回值、before=写前旧值。
    return {"ok": True, "key": key, "before": before,
            "resolved": ev.value, "readback": gate.snapshot.get(key)}


def _read_config(gate):
    snap = gate.snapshot
    return {"ok": True,
            "config": {k: snap[k] for k in CONFIG_SCHEMA if k in snap},
            "keys": {k: {kk: v.get(kk) for kk in ("type", "lo", "hi", "desc")}
                     for k, v in CONFIG_SCHEMA.items()}}


class PaperPilotRequiredSource:
    """MCP 投影的必填项注入来源（``mecha.providers.mcp.RequiredSource`` 协议）。

    ``execute`` 用 ``**kwargs`` 包装 ⇒ 框架从签名推必填得**空集**（必填会在模型面
    静默消失）。这里从**同一份声明表 + 能力自描述**求权威必填项，交给
    ``build_mcp_server(required_source=…)``——投影不靠实现细节反推。
    """

    def __init__(self, container) -> None:
        self._map: dict[str, list[str]] = {}
        for decl in TOOL_DECLS:
            cap_params = _cap_params(container, decl.cap_name)
            self._map[decl.mecha_name] = _required_names(cap_params, decl)

    def required_for(self, name: str, tool) -> list[str] | None:
        return self._map.get(name)


def build_required_source(container) -> PaperPilotRequiredSource:
    return PaperPilotRequiredSource(container)

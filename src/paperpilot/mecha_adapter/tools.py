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

import json
from collections.abc import Mapping
from dataclasses import dataclass

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
    """一条工具声明（mecha 名 → 能力名 + 工具级文案 + 参数处置）。

    参数描述**不在这里**（N8）：住能力层 ``ToolSpec.params[..]["description"]``，
    由 ``_derive_parameters`` 从 ``_cap_params`` 透传——避免第二份手抄漂移。
    """

    mecha_name: str
    cap_name: str
    kind: str                       # "read" | "write"
    description: str
    via_surface: bool = False        # True = 走 Engine.run（每日流水线）
    omit: tuple[str, ...] = ()       # 不作为模型可见参数的能力入参


#: 24 能力的投影表（单一来源：参数派生、必填注入、Surface 注册都由它驱动）。
#: 参数描述不在此（N8）——由 ``_derive_parameters`` 从能力 ``ToolSpec.params`` 透传。
TOOL_DECLS: tuple[ToolDecl, ...] = (
    # ------------------------------------------------------------ 只读面（10）
    ToolDecl("query_topics", "list_topics", "read",
             "列出研究主题：名称、关键词、分类白名单、配额、评分阈值、启用状态。"),
    ToolDecl("read_digest", "get_digest", "read",
             "读取某日简报全文（Markdown）与统计。"),
    ToolDecl("search_papers", "search_papers", "read",
             "在论文库中检索（标题/摘要/要点全文匹配）。query 省略则按时间倒序列出近期论文。"),
    ToolDecl("read_paper", "get_paper", "read",
             "论文详情：原文摘要、最新 AI 总结、打分历史、笔记、阅读态、本地 PDF 路径。"),
    ToolDecl("read_activity", "get_activity", "read",
             "归因面：①操作事件流（可按操作者/操作类型/序号过滤）②近期运行、AI 调用与简报统计。"),
    ToolDecl("review_status", "review_status", "read",
             "查看某天评审进度：候选数、已评审数、状态。"),
    ToolDecl("paper_metrics", "paper_metrics", "read",
             "一篇论文的影响力指标：被引数、参考数、高影响引用数、年份、发表场所、要点摘要。"
             "用于判断分量与质量信号。"),
    ToolDecl("read_references", "get_references", "read",
             "取一篇论文引用的文献（往前追溯技术起源）。默认按被引论文引用数降序——排最前的"
             "即起源/奠基候选；含引用意图与是否高影响引用。可对结果递归再查以继续往前追溯。"),
    ToolDecl("read_citations", "get_citations", "read",
             "取引用了这篇论文的文献（往后看影响力扩散与后续工作）。可按引用数降序。"),
    ToolDecl("query_briefings", "list_briefings", "read",
             "列出已归档的简报（日期、run_id、入选篇数、是否 AI、状态），按日期倒序。"
             "与 read_digest 分工：本工具给管理面（有哪些简报可删）；read_digest 给阅读面。"),
    ToolDecl("query_profile", "get_profile", "read",
             "读兴趣画像：arXiv 分类/词/作者三维权重 top + 分类熵（防茧房哨兵）。"
             "行为信号驱动，主题只是先验种子。"),
    ToolDecl("record_signal", "record_signal", "write",
             "记一个兴趣信号（view/outbound/download/star/read/skip/uninterested）；"
             "用户口头说'我下了/看了/不感兴趣'时用它声明，与站内实测信号同权。",
             omit=("actor",)),
    ToolDecl("feed_generate", "feed_generate", "read",
             "生成兴趣推荐流（四道召回：主兴趣/邻接桥/热点作者/探索，带道属与 why，确定性可复算）。"
             "只读预览用；要上面板/换页用 publish_feed。刷新由你定：换口味改 mix/quotas，换窗口改 days/seen_days，"
             "接着往下端用上次回执的 meta.next_offset；池子浅了就 fetch_papers 补货。用户说'想看点野的'⇒mix=explorer。"),
    ToolDecl("publish_feed", "publish_feed", "write",
             "发布 feed 一期（写库+审计）：/feed 面板即显示这期、不自行重算。"
             "用户说'20条60天/换一页/野一点'都再调一次本工具，参数全显式；"
             "换页 offset 续用上次回执 meta.next_offset（零重叠零空洞）。",
             omit=("actor",)),
    ToolDecl("write_summary", "write_summary", "write",
             "单篇补卡：对已入库论文直接写日报级卡（总结五段+可选 score/label 成对），"
             "feed 卡与详情页自动复用；写错可撤销。",
             omit=("actor",)),
    # ------------------------------------------------------------ 写入 / 运行面（14）
    ToolDecl("undo_change", "undo", "write",
             "撤销一条可逆的写入（seq=0=最近一条可逆操作）。入库与定稿不可逆，会明确说明。",
             omit=("actor",)),
    ToolDecl("fetch_papers", "fetch_papers", "write",
             "按主题抓取 arXiv 最近 N 天提交的新论文入库（遵守 arXiv 限速，可能较慢）。",
             omit=("actor",)),
    ToolDecl("fetch_paper_by_id", "fetch_paper_by_id", "write",
             "按 arXiv id 把单篇拉进入库（对话里‘这篇加进来’）；幂等，已在库回 cached。",
             omit=("actor",)),
    ToolDecl("prepare_review", "prepare_review", "write",
             "评审阶段1：取过规则后的候选清单（含主题画像、摘要截断、基线分），等待评审。"
             "两阶段（W5 省 token）：stage=brief 只看标题+短摘粗筛，再 stage=full+arxiv_ids "
             "拉 shortlist 全文精评；池子空时 requeue=True 可原班人马再审。",
             omit=("actor",)),
    ToolDecl("submit_review", "submit_review", "write",
             "评审阶段2：提交对候选的评审。reviews=[{arxiv_id,score,label,reason,tags?,summary?}]。",
             omit=("actor",)),
    ToolDecl("finalize_briefing", "finalize_briefing", "write",
             "评审阶段3：用已提交评审（缺的用基线分）筛选、精读、生成简报并落库。",
             omit=("actor",)),
    ToolDecl("run_pipeline", "run_pipeline", "write",
             "一键全流程（无外部评审）：候选→规则→程序化打分（未配模型时用启发式兜底）→简报。",
             via_surface=True, omit=("actor",)),
    ToolDecl("add_topic", "add_topic", "write",
             "新增研究主题（即时生效）。keywords/categories/exclude_keywords 用逗号分隔。",
             omit=("actor",)),
    ToolDecl("update_topic", "update_topic", "write",
             "更新既有主题：只改传入的字段（列表逗号分隔、替换语义，省略=不动；"
             "quota/threshold 负数=不动）。启用/停用请用 set_topic_enabled。",
             omit=("actor",)),
    ToolDecl("set_topic_enabled", "set_topic_enabled", "write",
             "启用或停用某个研究主题。",
             omit=("actor",)),
    ToolDecl("mark_read", "mark_read", "write",
             "标记论文为已读或未读。",
             omit=("actor",)),
    ToolDecl("star_paper", "star_paper", "write",
             "收藏或取消收藏论文。",
             omit=("actor",)),
    ToolDecl("skip_paper", "skip_paper", "write",
             "标记论文为不感兴趣（同类下次过滤）。",
             omit=("actor",)),
    ToolDecl("add_note", "add_note", "write",
             "给论文添加笔记（调研沉淀）。",
             omit=("actor",)),
    ToolDecl("delete_briefing", "delete_briefing", "write",
             "删除指定日期的简报（同日多版本一并删）。可撤销——事件里存了 markdown+stats 快照，"
             "undo_change(seq=0) 一键重建。Web 设置页的「删简报」按钮与此同源。",
             omit=("actor",)),
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
        desc = info.get("description")        # N8：描述从能力 spec 透传（单一来源，不再手抄）
        if desc:
            entry["description"] = desc
        out[pname] = entry
    return out


def _required_names(cap_params: Mapping[str, dict], decl: ToolDecl) -> list[str]:
    return [p for p, i in cap_params.items()
            if i.get("required") and p not in decl.omit]


def envelope_to_error(res: Mapping[str, object]) -> MechaError:
    """能力失败信封 → MechaError（读/写两路共用，单一来源）。

    ⚠ 非标准信封兜底（N2）：能力返 `ok:false` 却**不带 `error`** 时，绝不吞成
    一口“调用失败”——用 `bad_envelope` + 截断透传原始 JSON，让模型看得见到底返了
    什么。根子（“允许裸 ok:false”）已回馈框架（见 MECHA-N3）。

    ⚠ **与框架那条 `bad_envelope` 的关系：纵深，不是重复**（2026-09-26 框架第 3 批）。
    框架在**投影层**也做了同款（`mecha/providers/mcp.py`），它覆盖的是
    "**宿主自己没转换**"的通用情形；本仓在**适配层先转换**（这里就能看到原文，并把它透传给模型）
    ⇒ 框架那条对我们是**够不到的 backstop**（我们交出去的已是标准错误）。**故意保留**：
    删掉它，非标信封会退化成框架"回执 ok=False 却没给 error"那种**看不到原文**的失败，对模型更差。
    """
    e = res.get("error")
    if not isinstance(e, Mapping) or not e:
        raw = json.dumps(res, ensure_ascii=False, default=str)[:500]
        return MechaError(
            f"能力返回了非标准失败信封（ok:false 却无 error）：{raw}",
            kind="bad_envelope",
            hint="能力层失败须返回 {ok:false, error:{kind,message,hint}}；这是能力层的信封 bug。",
        )
    return MechaError(
        str(e.get("message") or "调用失败"),
        kind=str(e.get("kind") or "error"),
        hint=str(e.get("hint") or ""),
        suggest=_suggest_text(e.get("suggest")),
    )


def _raise_from_envelope(res: Mapping[str, object]) -> None:
    """把能力失败信封转成 MechaError 抛出（ToolRegistry.execute 会归一化）。"""
    raise envelope_to_error(res)


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
            execute = _make_command_bridge(sw.commands, sw.gate, channel, decl.mecha_name,
                                           approval=sw.approval)
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
    _register_authority_tool(reg, sw.authority)
    _register_job_tools(reg, sw, channel, container)
    return reg


def _make_capability_bridge(container, cap_name: str):
    """只读能力的 execute 桥（不经门；失败信封转 MechaError 抛出）。"""
    def _exec(**kwargs):
        res = capabilities.invoke(container, cap_name, **dict(kwargs))
        if not res.get("ok"):
            _raise_from_envelope(res)
        return res
    return _exec


def _make_command_bridge(commands, gate, channel: Channel, cmd_name: str, *, approval=None):
    """写入能力的 execute 桥：经命令面 invoke（authority 写权闸 + Gate 审计）。

    ``call_id`` 从请求作用域读并随 args 传入——命令面把它抽给 Gate 审计事件，
    把这次工具调用与宿主行为史钉在一起（总纲 §5.2② 互引）。经 ``invoke_command``
    统一入口调用（写权检查前置在框架侧；handler 的 channel 由 ``wants_channel`` 声明注入）。
    ``approval`` = 宿主的审批通道（``sw.approval``）：声明了 ``approval_required`` 的命令
    由框架 fail-closed 拦，本项目当前无此声明 ⇒ 不触发，但接口先接对。
    """
    from .commands import invoke_command  # 延迟导入：commands 依赖 tools，避免模块级循环

    def _exec(**kwargs):
        args = dict(kwargs)
        args["call_id"] = CURRENT_CALL_ID.get()
        # reason 在命令面是必填（才会透传给 handler、写进域 journal）；对模型仍可选，
        # 故省略时桥接填默认空串（不影响命令 required 契约）。
        args.setdefault("reason", "")
        res = invoke_command(commands, gate, channel, cmd_name, args, approval=approval)
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


def _authority_view(authority) -> dict:
    """写权自省（N5）：当前模式 + AI/人谁能写 + 怎么开。用 gate(side) 纯查询探侧（无副作用）。"""
    def _can(side: str) -> bool:
        try:
            authority.gate(side)          # 只判 mode与side 是否匹配，不改任何东西
            return True
        except Exception:  # noqa: BLE001 - GateDenied 即“不能写”
            return False
    mode = str(getattr(authority.mode, "value", authority.mode))
    return {"ok": True, "mode": mode,
            "ai_can_write": _can("ai"), "human_holds": _can("human"),
            "how_to_open": ("写权由人类侧授予：在 Web 的 /settings 「写权模式」卡点“授予 AI 写权”"
                            "（需控制口令），或用 `paperpilot serve/ai --open-gate` 启动。"
                            "AI 侧不能自解锁（单写权）。")}


def _register_authority_tool(reg: ToolRegistry, authority) -> None:
    """只读工具 read_authority：写工具被拒的 hint 会引用它（先查后写，不盲撞）。"""
    reg.register(define_tool(
        name="read_authority",
        description=(
            "读当前写权模式：AI 此刻能不能写、人在不在写、以及怎么开闸。"
            "发起写入前先查它，避免在锁定态下写了才被拒。单写权：同一时刻只一侧能写。"
        ),
        parameters={},
        output_schema={"type": "object", "required": ["ok", "mode", "ai_can_write"]},
        execute=lambda: _authority_view(authority),
        banned_words=_BANNED_WORDS,
    ))


def _register_job_tools(reg: ToolRegistry, sw, channel, container,
                       max_concurrent: int = 2) -> None:
    """长活 job 化（E2.1 / 缺口1）：submit/read/cancel，先接最慢的 run_pipeline。

    ⚠ 框架 local JobRegistry **每次 submit 新起一个 daemon 线程、不排队、无上限**——
    并发度必须项目自兜：这里用 `BoundedSemaphore` 兜住上限（超额响亮拒绝，不静默起线程）。
    job 体走 run_pipeline **命令面**（同一道写权门 + 审计 + engine 取消点），不是绕开门直跑。
    """
    import threading

    from .commands import invoke_command

    sem = threading.BoundedSemaphore(max_concurrent)
    commands, gate, jobs, authority = sw.commands, sw.gate, sw.jobs, sw.authority

    def _err(kind: str, message: str, hint: str = "") -> dict:
        return {"ok": False, "error": {"kind": kind, "message": message, "hint": hint}}

    def _submit(reason: str = "") -> dict:
        # ⚠ 这不是"治理闸"（治理检查已由框架在调 handler 之前做）；这里是**提交前的 UX 预检**：
        # 让 AI **同步**拿到拒绝（而不是提交一个注定被拒的 job、再去轮询失败）。它**之前**
        # 没有任何副作用（sem.acquire 在它之后）⇒ 不构成"改动落地才报审计失败"那类风险。
        # （保留与否的取舍已报主代理；若判定"多余守卫"，删掉它只影响拒绝的**时序**，不影响安全。）
        try:
            authority.gate(channel.side)
        except MechaError as e:
            return _err(e.kind, e.message, f"{e.hint} 先 read_authority 查写权。")
        if not sem.acquire(blocking=False):               # 项目兜并发上限（框架不给队列）
            return _err("job_limit", f"并发后台任务已满（上限 {max_concurrent}）",
                        "用 read_job 看进度、等某个跑完再提交；不要反复重试。")

        def _work(ctx):
            try:
                return invoke_command(commands, gate, channel, "run_pipeline",
                                      {"reason": reason or "后台流水线"}, context=ctx,
                                      approval=sw.approval)
            finally:
                sem.release()

        job = jobs.submit(_work, command="run_pipeline", cancel_supported=True)
        return {"ok": True, "job_id": job.id, "state": job.state.value,
                "hint": "read_job(job_id) 轮询进度/结果；cancel_job(job_id) 可取消"
                        "（协作式：跑到写库前取消才不产生副作用）"}

    def _read(job_id: str = "") -> dict:
        if not job_id:
            return {"ok": True, "jobs": jobs.list()}
        try:
            job = jobs.get(job_id)
        except MechaError as e:
            return _err(e.kind, e.message, e.hint)
        out: dict = {"ok": True, **job.status()}
        if job.state.value == "done":
            out["result"] = job.result
        elif job.state.value == "failed":
            out["error_detail"] = job.error
        return out

    def _cancel(job_id: str) -> dict:
        # 框架 `JobRegistry.cancel` 现在**回真话**（2026-09-26 第 3 批）：
        # `{job_id, state_before, terminal, cancel_requested}` ⇒ 本项目原先"先探 `job.state`
        # 再自己编文案"的兜底**已删**（框架 docstring 的判词：**一个什么都不告诉你的接口，
        # 就是在邀请调用方自己编**——而"对已跑完的 job 说取消已登记"就是编出来的谎话）。
        # 字段直接用框架的名字（同一事实一个名字）；终态/协作式的**语义由框架说**，不再由我们猜。
        try:
            res = jobs.cancel(job_id)
        except MechaError as e:
            return _err(e.kind, e.message, e.hint)      # 含未知 id / cancel_supported=False
        terminal = bool(res["terminal"])
        return {
            "ok": True,
            "job_id": res["job_id"],
            "state_before": res["state_before"],
            "terminal": terminal,
            "cancel_requested": res["cancel_requested"],
            "note": (f"任务已是终态（{res['state_before']}），无需取消" if terminal
                     else "取消已登记；协作式取消——已在写的会跑完，未开工的直接不写"),
        }

    reg.register(define_tool(
        name="submit_job",
        description="把每日流水线作为后台任务提交（不阻塞对话），立刻拿 job_id。配合 read_job 轮询、cancel_job 取消。",
        parameters={"reason": {"type": "string", "default": "",
                               "description": "一句话中文说明本次后台跑批目的"}},
        output_schema={"type": "object", "required": ["ok"]},
        execute=_submit, banned_words=_BANNED_WORDS,
    ))
    reg.register(define_tool(
        name="read_job",
        description="读后台任务状态：给 job_id 查单个（done 时带结果），不给则列全部。用于轮询进度。",
        parameters={"job_id": {"type": "string", "default": "",
                               "description": "submit_job 返回的任务 id（省略=列全部）"}},
        output_schema={"type": "object", "required": ["ok"]},
        execute=_read, banned_words=_BANNED_WORDS,
    ))
    reg.register(define_tool(
        name="cancel_job",
        description="请求取消一个后台任务（协作式：在安全点停下，不强杀线程）。",
        parameters={"job_id": {"type": "string", "description": "要取消的任务 id"}},
        output_schema={"type": "object", "required": ["ok"]},
        execute=_cancel, banned_words=_BANNED_WORDS,
    ))


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

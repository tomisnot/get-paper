"""PaperPilot 的写治理命令面（Phase 2）：论文库异构写 → mecha 命令。

Scheme E 的核心（交接文档 §6）：关系型论文库的异构写塞不进 mecha 的 KV History，
但它们仍可被**门治理 + cockpit 看见**——办法是把每个写能力注册成
``define_command(side_effect=True)``：

- handler 调 ``capabilities.invoke``（写 repo + repo.events 供 undo，域数据/undo 仍留 repo）；
- 命令面把 ``command.<name>`` + 不透明 ``result_ref`` 审计进 mecha History（操作者审计面）；
- 两份 journal 靠 ``result_ref``（arxiv_id/note_id/date/run_id…）互引，非 split-brain。

⭐ **authority 前置检查是必需的（n=3 发现）**：``CommandRegistry.invoke`` 在**跑完
handler 之后**才做 gate 审计（``_audit``），而 PaperPilot 的域写发生在 handler 内
（``capabilities.invoke`` 直写 SQLite，不经 gate）。若不在 handler 里先查写权，
LOCKED 态下**域写会先落地、审计才失败**（最该被拦的写反而无痕）。故 handler 第一步
``authority.gate(channel.side)``：LOCKED/模式不符 → ``GateDenied``，域写根本不发生。
"""

from __future__ import annotations

import contextvars

from mecha.commands import CommandResult, define_command
from mecha.errors import MechaError
from mecha.surface import ExecutionContext

from .. import capabilities
from .tools import TOOL_DECLS, _cap_params, envelope_to_error

#: 当前调用的操作者通道（contextvar，按请求/线程隔离）。
#: ⭐ 为何需要：``CommandRegistry.invoke(name, args, *, context, gate, channel)`` 把 channel
#: 用于 scope 检查与审计，但**不把 channel 传给 handler**（handler 只收 context= + 声明参数）。
#: 而 handler 需要用**实际调用方的通道**做两件事：① ``authority.gate(channel.side)``
#: 写权前置闸（human/ai 侧不同）；② ``actor=channel.actor`` 归因（否则人类写会被误记为 ai）。
#: 故由统一入口 ``invoke_command`` 在 invoke 前 set 本 contextvar，handler 读它。
_CURRENT_CHANNEL: contextvars.ContextVar = contextvars.ContextVar(
    "pp_command_channel", default=None)


def invoke_command(commands, gate, channel, cmd_name: str, args: dict,
                   context=None) -> dict:
    """统一命令调用口：把实际通道经 contextvar 传给 handler，再经框架 invoke。

    AI 工具桥（ai 通道）与 Web 人类面（human 通道）都走这里——handler 据此
    用**正确的 side/actor** 过写权闸与归因（人机同路、各记各的 actor）。
    """
    ctx = context if isinstance(context, ExecutionContext) else ExecutionContext()
    token = _CURRENT_CHANNEL.set(channel)
    try:
        return commands.invoke(cmd_name, args, context=ctx, gate=gate, channel=channel)
    finally:
        _CURRENT_CHANNEL.reset(token)

#: 命令 scope（声明字段；进审计记录供 cockpit/归因，未绑 ScopePolicy 则不做过滤——
#: 与 EL 单宿主同款。scope 是「能写但有作用域边界」的维度，留待需要细粒度时绑定）。
_SCOPES: dict[str, tuple[str, ...]] = {
    "download_paper": ("library",), "mark_read": ("library",),
    "star_paper": ("library",), "skip_paper": ("library",), "add_note": ("library",),
    "add_topic": ("topics",), "set_topic_enabled": ("topics",),
    "prepare_review": ("review",), "submit_review": ("review",),
    "finalize_briefing": ("review",),
    "run_pipeline": ("pipeline",), "fetch_papers": ("pipeline",),
    "undo_change": ("undo",),
}

#: result_ref 抽取的实体标识键（不透明引用，供两份 journal 互引；不放结果体）。
#: 注：repo.undo 回的键是 ``undone_seq``（非 undid_seq）——写错会让最该被追溯的
#: 撤销操作 result_ref=None、互引断裂（设计师复审发现③）。
_REF_KEYS = ("arxiv_id", "note_id", "topic", "added", "date", "run_id",
             "briefing_id", "seq", "undone_seq", "path")


def _scope_for(name: str) -> tuple[str, ...]:
    return _SCOPES.get(name, ("library",))


def _command_params(cap_params: dict, omit: tuple[str, ...]) -> dict:
    """从能力自描述派生命令的 JSON-schema 参数（properties/required）。

    ⭐ **reason 显式声明为必填**（设计师复审发现①）：mecha 把 ``reason``/``call_id``
    当 ``RESERVED_ARGS``，**不在 required 里就会在进 handler 前被剥掉**（只进审计 meta）。
    若不声明，handler 收不到 reason → ``capabilities.invoke(reason="")`` → 域 journal
    （repo.events）的 reason 永远为空，/activity 与 undo 归因丢了“为什么”。故把 reason
    加进 required（框架的 ``declared_set`` 才会把它透传给 handler）——工具层对模型仍
    可选，桥接填默认空串。

    连带好处：required 恒非空 ⇒ 避开框架“空/缺失 required 回退成全 properties 必填”
    的坑（MECHA-N3 发现 6），故所有命令都能正常声明 properties（不再需 additionalProperties 宽松形）。
    """
    props: dict[str, dict] = {}
    required: list[str] = []
    for pname, info in cap_params.items():
        if pname in omit:
            continue
        entry: dict[str, object] = {}
        if info.get("type"):
            entry["type"] = info["type"]
        props[pname] = entry
        if info.get("required"):
            required.append(pname)
    if "reason" in props and "reason" not in required:
        required.append("reason")        # 让 handler 收到操作者的“为什么”（写进域 journal）
    if not required:                     # 防御：无 reason 也无必填的能力→宽松形（不应发生）
        return {"type": "object", "additionalProperties": True}
    return {"type": "object", "properties": props, "required": required}


def _result_ref(res: dict) -> dict | None:
    ref = {k: res[k] for k in _REF_KEYS if k in res and res[k] is not None}
    return ref or None


def _estimate(cap_name: str, container):
    """每条写命令的耗时预估（E2.2 / 缺口2）：**非零**，让 AI 能据此判断要不要 job 化。

    网络/重计算类（run_pipeline/fetch_papers）给 **callable**（按参数现算：启用主题数
    × 回溯天数 × arXiv ~3s 限速）；本地快写（写库/文件）给小常量。不再一律 0.0。
    """
    st = container.settings
    n_topics = sum(1 for t in st.topics if t.enabled) or 1
    lookback = max(1, int(st.lookback_days))
    if cap_name == "run_pipeline":
        return lambda _args: float(n_topics * lookback * 3 + 5)
    if cap_name == "fetch_papers":
        return lambda args: float(max(1, int(args.get("days") or lookback)) * n_topics * 3)
    if cap_name == "download_paper":
        return 10.0                       # 拉一份 PDF：网络 + 落盘
    return 1.0                            # 本地写（评审/笔记/主题…）：小而非零


def _make_handler(container, decl, authority, default_channel, surface):
    """命令体：① authority 写权前置闸 → ② 调能力/Engine.run → ③ 归一化回执。

    通道取自 ``_CURRENT_CHANNEL``（实际调用方：ai 工具桥 / human Web），缺失时回落
    ``default_channel``——确保写权闸看**正确的 side**、归因用**正确的 actor**。
    """
    cap_name = decl.cap_name
    via_surface = decl.via_surface

    def handler(context=None, **args):
        channel = _CURRENT_CHANNEL.get() or default_channel
        # ① 写权前置（见模块 docstring 的 n=3 说明）：LOCKED/模式不符 → GateDenied，
        #    域写不发生；拒绝的 hint 引向只读工具 read_authority（N5：先查后写，不盲撞）。
        try:
            authority.gate(channel.side)
        except MechaError as e:
            raise MechaError(
                e.message, kind=e.kind,
                hint=f"{e.hint} 先用只读工具 read_authority 查当前写权模式（how_to_open 说怎么开）。",
                suggest=e.suggest or "read_authority",
            ) from None
        args = dict(args)
        args.pop("actor", None)                 # actor 由通道钉死，绝不接受请求传入
        args["actor"] = channel.actor
        if via_surface:
            ctx = context if isinstance(context, ExecutionContext) else ExecutionContext()
            res = surface.run(args, context=ctx)
        else:
            res = capabilities.invoke(container, cap_name, **args)
        if not res.get("ok"):
            # 失败经共享的 envelope_to_error（含 N2 的 bad_envelope 兜底，读写两路一致）。
            return CommandResult(ok=False, values={"ok": False}, error=envelope_to_error(res))
        return CommandResult(ok=True, values=dict(res), result_ref=_result_ref(dict(res)))

    return handler


def build_commands(container, sw, channel=None) -> list[str]:
    """把 13 个写能力注册成命令（进 ``sw.commands``）。返回注册的命令名清单。

    ``channel`` 缺省 = ``sw.channels['ai']``；handler 捕获它做 actor 钉死与写权判定。
    """
    channel = channel or sw.channels["ai"]
    commands = sw.commands
    names: list[str] = []
    for decl in TOOL_DECLS:
        if decl.kind != "write":
            continue
        cap_params = _cap_params(container, decl.cap_name)
        spec = define_command(
            name=decl.mecha_name,
            description=decl.description,
            parameters=_command_params(cap_params, decl.omit),
            output_schema={"type": "object", "required": ["ok"]},
            side_effect=True,
            scope=_scope_for(decl.mecha_name),
            estimate_sec=_estimate(decl.cap_name, container),
            cancel_supported=decl.via_surface,
            approval_required=False,
        )
        commands.register(spec, _make_handler(
            container, decl, sw.authority, channel, sw.surface))
        names.append(decl.mecha_name)
    return names

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

from mecha.commands import CommandResult, define_command
from mecha.errors import MechaError
from mecha.surface import ExecutionContext

from .. import capabilities
from .tools import TOOL_DECLS, _cap_params, _suggest_text

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
_REF_KEYS = ("arxiv_id", "note_id", "topic", "added", "date", "run_id",
             "briefing_id", "seq", "undid_seq", "path")


def _scope_for(name: str) -> tuple[str, ...]:
    return _SCOPES.get(name, ("library",))


def _command_params(cap_params: dict, omit: tuple[str, ...]) -> dict:
    """从能力自描述派生命令的 JSON-schema 参数（properties/required）。

    ⚠ **零必填命令用宽松形**（n=3 发现）：``CommandRegistry._check_args`` 在
    ``required`` 为空/缺失时**回退成「所有 properties 都必填」**——故只有可选参数的
    命令（fetch_papers/run_pipeline/prepare_review/finalize_briefing/undo_change）
    无法同时声明 properties 与「零必填」，只能用 ``additionalProperties: True``
    放弃 properties 声明。模型面的参数契约由**工具层**（define_tool + RequiredSource）
    保证，域参数校验由 ``capabilities.invoke`` 兜底，故命令层宽松不丢正确性。
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
    if required:
        return {"type": "object", "properties": props, "required": required}
    return {"type": "object", "additionalProperties": True}


def _result_ref(res: dict) -> dict | None:
    ref = {k: res[k] for k in _REF_KEYS if k in res and res[k] is not None}
    return ref or None


def _make_handler(container, decl, authority, channel, surface):
    """命令体：① authority 写权前置闸 → ② 调能力/Engine.run → ③ 归一化回执。"""
    cap_name = decl.cap_name
    via_surface = decl.via_surface

    def handler(context=None, **args):
        # ① 写权前置（见模块 docstring 的 n=3 说明）：LOCKED/模式不符 → GateDenied，
        #    域写不发生；命令面把 MechaError 归一化成失败回执（可归因）。
        authority.gate(channel.side)
        args = dict(args)
        args.pop("actor", None)                 # actor 由通道钉死，绝不接受请求传入
        args["actor"] = channel.actor
        if via_surface:
            ctx = context if isinstance(context, ExecutionContext) else ExecutionContext()
            res = surface.run(args, context=ctx)
        else:
            res = capabilities.invoke(container, cap_name, **args)
        if not res.get("ok"):
            e = res.get("error") or {}
            return CommandResult(
                ok=False, values={"ok": False},
                error=MechaError(
                    str(e.get("message") or "写入失败"),
                    kind=str(e.get("kind") or "error"),
                    hint=str(e.get("hint") or ""),
                    suggest=_suggest_text(e.get("suggest")),
                ),
            )
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
            estimate_sec=0.0,
            cancel_supported=decl.via_surface,
            approval_required=False,
        )
        commands.register(spec, _make_handler(
            container, decl, sw.authority, channel, sw.surface))
        names.append(decl.mecha_name)
    return names

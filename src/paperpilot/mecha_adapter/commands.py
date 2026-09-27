"""PaperPilot 的写治理命令面（Phase 2）：论文库异构写 → mecha 命令。

Scheme E 的核心（交接文档 §6）：关系型论文库的异构写塞不进 mecha 的 KV History，
但它们仍可被**门治理 + cockpit 看见**——办法是把每个写能力注册成
``define_command(side_effect=True)``：

- handler 调 ``capabilities.invoke``（写 repo + repo.events 供 undo，域数据/undo 仍留 repo）；
- 命令面把 ``command.<name>`` + 不透明 ``result_ref`` 审计进 mecha History（操作者审计面）；
- 两份 journal 靠 ``result_ref``（arxiv_id/note_id/date/run_id…）互引，非 split-brain。

⭐ **写权前置检查现在由框架做（2026-09-26 起）**：本条曾是 n=3 报告的**发现 7**——
``CommandRegistry.invoke`` 原先**跑完 handler 才**做 gate 审计，而 PaperPilot 的域写发生在
handler 内（``capabilities.invoke`` 直写 SQLite）⇒ LOCKED 态下**域写先落地、审计才失败**。
当时项目在 handler 第一步手写 ``authority.gate(channel.side)`` 自救；**框架已把检查前置**
（`Gate.check` 与 `Gate.set` 共用同一份判定，``invoke`` 在调 handler 之前就查）⇒
**那份手写闸已删**（留着就是"同一事实两个守卫"）。归因也改成**声明式**：命令声明
``wants_channel=True`` ⇒ handler 直接收本次的 ``channel=``（不再用 contextvar 偷渡）。
"""

from __future__ import annotations

from mecha.commands import CommandResult, define_command
from mecha.surface import ExecutionContext

from .. import capabilities
from ..domain.pipeline import pipeline_config_override
from .engine import gate_scoring_override
from .tools import TOOL_DECLS, _cap_params, envelope_to_error

#: 写权被拒的失败档（框架 ``Authority.gate`` 的两个 kind）。
_WRITE_DENIED_KINDS = ("authority_locked", "authority_mode_mismatch")


def _point_denial_to_read_authority(res: dict) -> dict:
    """写权被拒时，把 hint/suggest 指到**本项目**的只读工具 ``read_authority``。

    为什么这**不是**"手写闸"：**判定完全是框架的**（``invoke`` 在调 handler 之前
    ``gate.check(channel)``），本项目只补一件框架不知道的事——**我们自己的工具叫什么**
    （N5 的"先查后写"：被拒时告诉 AI 去哪儿看当前写权模式与 how_to_open）。
    这段文字原先写在 **13 个 handler** 的 ``try/except`` 里，跟着手写前置闸一起收进
    **唯一入口**（13 份变 1 份；框架的原始 ``suggest`` 也折进 hint，不丢信息）。
    """
    if not res.get("is_error"):
        return res
    info = (res.get("error") or {}).get("info") or {}
    if info.get("kind") not in _WRITE_DENIED_KINDS:
        return res
    hint = str(info.get("hint") or "")
    if "read_authority" in hint:          # 幂等：叠两遍没有意义
        return res
    framework_suggest = str(info.get("suggest") or "")
    info["hint"] = (f"{hint} 先在 AI 侧用只读工具 read_authority 查当前写权模式"
                    f"（它的 how_to_open 说怎么开；人类侧：{framework_suggest}）。").strip()
    info["suggest"] = "read_authority"
    return res


def invoke_command(commands, gate, channel, cmd_name: str, args: dict,
                   context=None, approval=None) -> dict:
    """统一命令调用口：AI 工具桥（ai 通道）与 Web 人类面（human 通道）都走这里。

    两条纪律都在**框架侧**，本口只做透传 + 一件项目专有的补白：
    * **写权检查前置**：框架在调 handler 之前 ``gate.check(channel)`` ⇒ 域写（本项目 SQLite）
      根本不会发生（发现 7 的治本修法，见模块 docstring）；
    * **归因不靠猜**：命令声明 ``wants_channel=True`` ⇒ handler 收到本次 ``channel=``；
    * ⭐ ``approval=`` 透传给框架的**审批闸**（声明了 ``approval_required`` 的命令由框架
      fail-closed 拦；本项目**当前没有命令声明它**，故这一路今天不触发——但接线先做对，
      将来某条命令写上声明即生效，不用再改调用面）。
      调用方注入 ``sw.approval``（框架"不自动接线"是刻意的：自动接会让没声明审批的命令
      也走一遍审批通道 = 扩大行为面）。
    """
    ctx = context if isinstance(context, ExecutionContext) else ExecutionContext()
    return _point_denial_to_read_authority(
        commands.invoke(cmd_name, args, context=ctx, gate=gate, channel=channel,
                        approval=approval))


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

    连带好处：required 恒非空。⚠ **注意语义已翻转（2026-09-26，ADR「命令面治理闸前置与
    声明式 opt-in」）**：框架现在把 ``required`` 的**缺失与空都当"无必填"**（对齐 JSON
    Schema）；老语义是"缺失 ⇒ 全 properties 必填"。本函数**显式列出** required，故两种语义
    下行为一致（要必填就列出来）；而**空 required 不再需要宽松形兜底**——原来那支
    ``additionalProperties: True``（且**丢掉 properties**）在新语义下是**有害的**：
    它会让"无必填"的命令**连参数清单都投影不出去**。故删除该分支，恒回带 properties 的完整形状。
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
    # ⚠ 即使 `required` 为空也要带上 `properties`（新语义下空 = 无必填，正是我们想要的；
    #   旧写法在这里回退成"只有 additionalProperties、没有 properties"的宽松形——
    #   那会让 AI **看不到任何参数**，是潜伏的真缺陷）。
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


def _make_handler(container, decl, surface, gate):
    """命令体：① gate 配置覆盖下调能力/Engine.run → ② 归一化回执。

    通道由**框架声明式注入**（命令声明 ``wants_channel=True`` ⇒ handler 收 ``channel=``）：
    写权闸看正确的 side（框架前置检查）、归因用正确的 actor（人类写不再被误记成 ai——发现 9）。
    ⚠ **不设回落通道**：拿不到 channel 宁可**响亮地崩**，也不要静默回落成 ai 通道——
    那正是发现 9 的病（人类写被误归因）。本函数不再需要 ``authority``/``default_channel``。

    ⭐ N15：配置态在**这道边界**注入线程局部覆盖（set_config 写 gate 快照）——
    不只 run_pipeline，prepare/submit/finalize 各段同样吃到，杜绝静默 no-op。
    """
    cap_name = decl.cap_name
    via_surface = decl.via_surface

    def handler(context=None, channel=None, **args):
        args = dict(args)
        args.pop("actor", None)                 # actor 由通道钉死，绝不接受请求传入
        args["actor"] = channel.actor           # channel=None ⇒ 这里就炸（契约破了要响，不静默）
        # ⭐ N15：gate 快照作为本次调用的线程局部配置覆盖（与 Engine.run 同源函数，
        # 嵌套无害：context manager 保存/恢复上一层）。
        scoring, lookback = gate_scoring_override(container, gate)
        with pipeline_config_override(scoring=scoring, lookback_days=lookback):
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


def build_commands(container, sw) -> list[str]:
    """把 13 个写能力注册成命令（进 ``sw.commands``）。返回注册的命令名清单。

    ⚠ **不再传通道**：命令声明 ``wants_channel=True``，由框架在每次调用时注入**本次**的
    通道（这才是正确归因——装配期捕获一个默认通道正是发现 9 的成因）。
    """
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
            # 归因靠**声明**（不靠 contextvar 偷渡、也不靠签名自省）：声明后 handler 收本次
            # `channel=`，于是"谁调的"不再靠装配期捕获的默认通道猜（发现 9）。
            wants_channel=True,
            # ⚠ 刻意**不写** `approval_required`：它是"要不要人工批准"的**声明**，本项目
            # 没有需要审批的命令 ⇒ 按 ADR"其实不需要审批的命令请去掉这个声明（别留一个
            # 不干活的声明）"，连 `=False` 也不留（那是默认值，写了就是装饰）。
        )
        commands.register(spec, _make_handler(
            container, decl, sw.surface, sw.gate))
        names.append(decl.mecha_name)
    return names

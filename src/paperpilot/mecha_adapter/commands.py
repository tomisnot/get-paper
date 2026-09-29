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


#: 命令 scope（声明字段）。⚠ **过滤靠它 + 装配期绑定的 `ScopePolicy`**（见 `.scopes` 模块）：
#: 只声明不绑 = 声明是装饰（框架 `commands.py:432` 那段只在注入后才生效）。
#: ⚠ **没写进本表的命令会回落到 `("library",)`**（`_scope_for` 的默认）——
#: `update_topic` 从前就吃这个默认 ⇒ 与同族的 `add_topic`/`set_topic_enabled`（`topics`）
#: **口径不一致**；2026-09-26 显式补上。新增命令时**别依赖默认**。
_SCOPES: dict[str, tuple[str, ...]] = {
    "mark_read": ("library",),
    "star_paper": ("library",), "skip_paper": ("library",), "add_note": ("library",),
    "add_topic": ("topics",), "set_topic_enabled": ("topics",), "update_topic": ("topics",),
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


#: `set_config` 命令的参数契约（手写：它不对应任何能力自描述）。
#: ⚠ **形状必须是 JSON-schema 形**（`{type, properties, required}`）——与 `_command_params`
#: 的返回值同一形状；写成扁平的 `{name: {...}}` 会被框架判成"参数未声明"（我第一版就踩了）。
#: ⚠ `reason` **必须列进 `required`**：框架把 `reason`/`call_id` 当 `RESERVED_ARGS`，
#: **不声明就会在进 handler 之前被剥掉**（只进审计 meta）——与 18 条写命令同一纪律
#: （工具面对模型仍可选，由桥接填默认串）。
_CONFIG_PARAMS: dict[str, object] = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "description": "可写配置键（见 read_config 的清单）"},
        "value": {"description": "新值，类型须匹配该键（见 read_config）"},
        "reason": {"type": "string", "description": "一句话中文说明本次改动目的"},
    },
    "required": ["key", "value", "reason"],
}


def _make_config_handler(gate):
    """`set_config` 的命令体：**复用** `tools._set_config`（不复制第二份收编/校验/回执）。

    失败（`authority_locked` / `unknown_key` / `bad_value` …）**原样抛出** ⇒ 由框架转成
    **同一 kind** 的 failure，与它直写 `gate.set` 时的档位一致
    （判据 `test_set_config_gated` / `test_unknown_config_key_is_teachable` **未改仍绿**）。
    """
    from .tools import _set_config  # 延迟导入：commands ↔ tools 的既有依赖方向

    def handler(context=None, channel=None, **args):
        return CommandResult(
            ok=True,
            values=_set_config(gate, channel, args["key"], args["value"],
                               str(args.get("reason") or "")))

    return handler


#: `reset_profile` 命令的参数契约（手写）。`kind` 可省（=重置全部）；`reason` 必须进 required。
_PROFILE_PARAMS: dict[str, object] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string",
                 "description": "重置范围：category | term | author；省略或空 = 全部"},
        "reason": {"type": "string", "description": "一句话中文说明本次重置目的"},
    },
    "required": ["reason"],
}


def _make_profile_handler(container):
    """`reset_profile` 的命令体：**复用能力层**（`capabilities.invoke`），不复制第二份逻辑。

    ⚠ 这条命令**只给人**：它**不在 `TOOL_DECLS` 里** ⇒ AI 工具面没有它
    （`reset_profile` 是"改自己的标尺"那类动作，原设计刻意不给 AI）。
    命令面的价值在于：人类入口也走**同一道门**（写权 + `command.reset_profile` 审计）。
    """
    from .. import capabilities

    def handler(context=None, channel=None, **args):
        res = capabilities.invoke(container, "reset_profile",
                                  kind=str(args.get("kind") or ""),
                                  actor=channel.actor,
                                  reason=str(args.get("reason") or ""))
        if not res.get("ok"):
            return CommandResult(ok=False, values={"ok": False},
                                 error=envelope_to_error(res))
        return CommandResult(ok=True, values=dict(res))

    return handler


#: `delete_note` 的参数契约（手写）。人类专属（不进 `TOOL_DECLS`）。
_NOTE_DELETE_PARAMS: dict[str, object] = {
    "type": "object",
    "properties": {
        "note_id": {"type": "integer", "description": "要删除的笔记 id"},
        "reason": {"type": "string", "description": "一句话中文说明本次删除目的"},
    },
    "required": ["note_id", "reason"],
}


def _make_note_delete_handler(container):
    """`delete_note` 的命令体：复用 domain 服务（`container.retrieval.delete_note`）。

    ⚠ **人类专属**：本命令**不进 `TOOL_DECLS`** ⇒ AI 工具面里没有它（原设计意图：
    "删笔记是人类独有的管理操作、不与 AI 争写"）。命令面的价值是让**人**的删除也走门 + 留审计。
    """
    def handler(context=None, channel=None, **args):
        note_id = int(args["note_id"])
        container.retrieval.delete_note(
            note_id, actor=channel.actor,
            reason=str(args.get("reason") or "Web 面板删笔记"))
        return CommandResult(ok=True, values={"ok": True, "note_id": note_id})

    return handler


#: `delete_graph_view` 命令的参数契约（手写）。人类专属（不进 `TOOL_DECLS`）。
#: 注：`set_default_view` 无需手写契约——它在 `TOOL_DECLS` 里，参数由能力声明机械派生。
_VIEW_DELETE_PARAMS: dict[str, object] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "要删除的视图名"},
        "reason": {"type": "string", "description": "一句话中文说明为什么删"},
    },
    "required": ["name", "reason"],
}


def _make_cap_handler(container, cap_name: str, fields: tuple[str, ...]):
    """通用命令体：按字段名**转调能力层**（不复制第二份逻辑）。

    与 `_make_profile_handler` 同款，只是字段可变——省得每条"人类入口"再抄一遍 handler。
    `actor` 由**通道**钉死（human 侧恒 human），命令自身不改归因。
    """
    from .. import capabilities

    def handler(context=None, channel=None, **args):
        kw = {f: args.get(f) for f in fields if f != "reason"}
        kw["actor"] = channel.actor
        kw["reason"] = str(args.get("reason") or "")
        res = capabilities.invoke(container, cap_name, **kw)
        if not res.get("ok"):
            return CommandResult(ok=False, values={"ok": False},
                                 error=envelope_to_error(res))
        return CommandResult(ok=True, values=dict(res))

    return handler


#: `set_config_batch` 的参数契约（手写）。`items` = {键: 值}；`reason` 必须进 required。
_CONFIG_BATCH_PARAMS: dict[str, object] = {
    "type": "object",
    "properties": {
        "items": {"type": "object", "description": "{可写配置键: 新值}（整批原子）"},
        "reason": {"type": "string", "description": "一句话中文说明本次批量改动目的"},
    },
    "required": ["items", "reason"],
}


def _make_config_batch_handler(gate):
    """`set_config_batch` 的命令体：整批一次 `gate.set_batch`（**原子**）。

    ⚠ 值的收编（表单来的都是字符串 ⇒ 数值键要转回数值）**复用 `tools._coerce_config`**，
    不复制第二份。失败（未知键 / 越界 / 写权）**原样抛出** ⇒ 框架转成同一档失败，
    且因为 `set_batch` 是"先全校验再全落地"⇒ **一个键非法，整批一个都不落地**。
    """
    from .tools import CURRENT_CALL_ID, _coerce_config

    def handler(context=None, channel=None, **args):
        items = dict(args.get("items") or {})
        coerced = {k: _coerce_config(k, v) for k, v in items.items()}
        events = gate.set_batch(channel, coerced, str(args.get("reason") or ""),
                                call_id=CURRENT_CALL_ID.get())
        return CommandResult(ok=True, values={"ok": True, "count": len(events),
                                              "keys": sorted(coerced)})

    return handler


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
    """把写能力注册成命令（进 ``sw.commands``）。返回注册的命令名清单。

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

    # ⭐ **配置写也进命令面**（2026-09-26）：`set_config` 曾是**唯一直写 `gate.set` 的写工具**
    # （见 `tools.py::_register_config_tools`）⇒ 框架账上只有"某个配置键变了"这条**键事件**，
    # **没有"谁执行了一次 set_config"这条操作记录**。这里补一条命令，把那一半补上。
    # ⚠ handler **直接复用 `tools._set_config`**（收编 `_coerce_config` + 校验 `gate.set` +
    # 回执 `ok/key/before/resolved/readback`，**一行都不复制**）；失败原样抛出 ⇒ 档位不变。
    commands.register(define_command(
        name="set_config",
        description="写一个可写标量配置键（类型/值域由 Gate 校验）；改动会留操作审计。",
        parameters=dict(_CONFIG_PARAMS),
        output_schema={"type": "object", "required": ["ok"]},
        side_effect=True,
        scope=("config",),
        estimate_sec=0.5,              # 本地单键写：小而非零（与写能力同一纪律）
        cancel_supported=False,
        wants_channel=True,            # handler 要 channel：`gate.set` 的写权看 side
    ), _make_config_handler(sw.gate))
    names.append("set_config")

    # ⭐ **删视图的命令**（2026-09-29）：画出来的图是"作品"，要能像简报一样随时拉出来看、改、删。
    #  注：`set_default_view`（切首屏渲染哪张）**不需要在这里登记**——它是投影过的写工具，
    #  上面的 `for decl in TOOL_DECLS` 已经自动给它注册了命令（scope 落到缺省的 `library`，两侧放行）。
    #  而 `delete_graph_view` **不在 TOOL_DECLS 里**（AI 工具面没有它）⇒ 必须手写一条命令，
    #  让人的按钮也走同一道门，并用 `scope=views`（只授予 human）把"删除归人"落成规则。
    commands.register(define_command(
        name="delete_graph_view",
        description="删除一张已发布的图视图（人类专属：作品的处置权归人；可 undo 撤销）。",
        parameters=dict(_VIEW_DELETE_PARAMS),
        output_schema={"type": "object", "required": ["ok"]},
        side_effect=True,
        scope=("views",),
        estimate_sec=0.3,
        cancel_supported=False,
        wants_channel=True,
    ), _make_cap_handler(container, "delete_graph_view", ("name", "reason")))
    names.append("delete_graph_view")

    # ⚠ **`reset_profile` 是"人类专属"，这条命令只为给人一个入口**（2026-09-26）：
    # 它是"重置兴趣画像锚点"——让 AI 自助改锚点等于让它改自己的标尺 ⇒ 本仓**刻意不把它
    # 登记进 `TOOL_DECLS`**（AI 工具面里没有它，见 `tools.py` 的原注释）。但从前**人也没有入口**
    # ⇒ 这里补一条命令，让 Web 的按钮能走**同一道门**（写权 + `command.reset_profile` 审计）。
    commands.register(define_command(
        name="reset_profile",
        description="重置兴趣画像（人类专属：清掉行为学出来的锚点，回到出厂先验）。",
        parameters=dict(_PROFILE_PARAMS),
        output_schema={"type": "object", "required": ["ok"]},
        side_effect=True,
        scope=("profile",),
        estimate_sec=0.5,
        cancel_supported=False,
        wants_channel=True,
    ), _make_profile_handler(container))
    names.append("reset_profile")

    # ⭐ **批量配置写（第 5 件：修"两个真相源"）**：`/settings/general` 的 6 个标量键
    # **本来就是 Gate 状态**（`CONFIG_SCHEMA` ⇒ `gate.seed` 种进快照、`gate_scoring_override`
    # 让流水线以**快照**为准），而 Web 从前**直写 YAML+内存** ⇒ **绕过了它们自己的权威**
    # （面板 `/config`、`set_config`、流水线看快照；Web 改的是 YAML ⇒ 两个真相源）。
    # 这里补一条**批量命令**（内部 `gate.set_batch`）⇒ 保住"整表单一次提交"的 UX，
    # 且拿到 `set_batch` 的**原子语义**（任一键非法 ⇒ 一个都不落地）+ `command.set_config_batch` 审计。
    commands.register(define_command(
        name="set_config_batch",
        description="批量写可写标量配置键（整批原子：任一键非法则一个都不落地）。",
        parameters=dict(_CONFIG_BATCH_PARAMS),
        output_schema={"type": "object", "required": ["ok"]},
        side_effect=True,
        scope=("config",),
        estimate_sec=0.5,
        cancel_supported=False,
        wants_channel=True,
    ), _make_config_batch_handler(sw.gate))
    names.append("set_config_batch")

    # ⚠ **`delete_note` 同款：人类专属**（原注释："删笔记是人类独有的管理操作、不与 AI 争写"）
    # ⇒ 命令做了、**不进 `TOOL_DECLS`** ⇒ AI 侧仍然没有它；补的是"人的删除也走门 + 留审计"。
    commands.register(define_command(
        name="delete_note",
        description="删除一条笔记（人类专属：管理动作，不与 AI 争写）。",
        parameters=dict(_NOTE_DELETE_PARAMS),
        output_schema={"type": "object", "required": ["ok"]},
        side_effect=True,
        scope=("library",),
        estimate_sec=0.5,
        cancel_supported=False,
        wants_channel=True,
    ), _make_note_delete_handler(container))
    names.append("delete_note")
    return names

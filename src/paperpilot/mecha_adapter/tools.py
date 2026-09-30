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

    # ⚠ **已切投影**的这批写条目（见 _PROJECTED_TOOLS）在这里只为**命令面**（build_commands 也遍历本表）而留；**工具面**由 project() 生成（不再手写）。
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
    ToolDecl("sync_citations", "sync_citations", "write",
             "把一篇在库论文的引用边落库成本地图谱（重跑幂等）；之后上游簇/共引相似本地免费查。",
             omit=("actor",)),
    ToolDecl("sync_cited_by", "sync_cited_by", "write",
             "反查'谁引用了这篇'入图（下游扩散独立成层）；引用者可未入库，增量幂等不可 undo。",
             omit=("actor",)),
    ToolDecl("tag_paper", "tag_paper", "write",
             "给在库论文钉图论标签（平台源头/理论源头/综述枢纽/实验谱系/下游扩散/动机），"
             "一论文一枚；/network 图例着色靠它；可撤销。",
             omit=("actor",)),
    ToolDecl("tag_papers", "tag_papers", "write",
             "批量钉标签（items='arxiv:标签' 逗号分隔，一次事件可整批撤销）——几十篇别一篇一个调用。",
             omit=("actor",)),
    ToolDecl("query_tags", "query_tags", "read",
             "读回已钉标签（篇目→标签 + 各标签计数）：审计与二次编排用。"),
    ToolDecl("set_graph_view", "set_graph_view", "write",
             "发布一张图视图——/network 无参数打开即渲染它（AI 画什么，页面显示什么）："
             "根/深度/布局/分组/着色/标签/预算/锚点一次定完，即时生效、可撤销。",
             omit=("actor",)),
    ToolDecl("query_graph_views", "query_graph_views", "read",
             "列出已发布的图视图（名字/默认/spec 摘要）：切默认或复用前先看这里。"),
    ToolDecl("set_default_view", "set_default_view", "write",
             "把某张已发布视图设为默认（/network 无参数渲染它）。",
             omit=("actor",)),
    ToolDecl("materialize_view", "materialize_view", "write",
             "把视图里还没入库的点全部入库（幂等，可反复调用直到 remaining=0）——"
             "图上的每篇论文都应是库内论文，才能点进管理页/补卡/喂画像。",
             omit=("actor",)),
    ToolDecl("upstream_clusters", "upstream_clusters", "read",
             "关键上游簇：库内多篇反复引同一文献⇒领域思想源头。"),
    ToolDecl("related_papers", "related_papers", "read",
             "共引相似：与指定论文引用集交集最大的库内论文。"),
    ToolDecl("coverage_report", "coverage_report", "read",
             "调研资产覆盖率：有卡/读过/收藏统计 + 最新缺卡清单（write_summary 的工单）。"),
    ToolDecl("stats_timeseries", "stats_timeseries", "read",
             "趋势聚合：每日入库/信号漏斗/简报节奏/AI 成本按用途（近 N 天）。"),
    ToolDecl("watch_authors", "watch_authors", "read",
             "作者监控：主题关注+画像正权作者的近 N 天新提交（只读；入库逐篇 fetch_paper_by_id）。"),
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
    # ------------------------------------------------------------ 精读面（10）
    # arXiv HTML 正文 + 带位置的批注。分工：判断归 AI（读哪节、标哪句、批注写什么），
    # 几何归代码（原句→精确字符区间、资源离线化、页面渲染、无头截图）。
    # ⚠ `delete_mark` **不在这里**——批注是精读痕迹，处置权归人（与 delete_graph_view 同款）。
    ToolDecl("fetch_paper_html", "fetch_paper_html", "write",
             "下载并归档一篇论文的 HTML 正文（剥脚本、全量离线抓下样式与图片）："
             "精读体系的地基。arXiv 未提供 HTML 的论文如实报 no_html。",
             omit=("actor",)),
    ToolDecl("read_paper_outline", "read_paper_outline", "read",
             "一篇论文的章节树 + 锚点地图：每节的块数与类型分布、块 id 样例。"
             "进正文前先看它——后面的读/检索/批注都吃这些块 id。"),
    ToolDecl("search_library_text", "search_library_text", "read",
             "在已归档正文里跨篇检索（块级）：命中回论文 + 块 + 高亮片段，可一步跳到原文那一段。"
             "与 search_papers 互补——那个答「哪篇相关」，这个答「原文在哪说」。"),
    ToolDecl("read_paper_text", "read_paper_text", "read",
             "读论文正文（分块、带块 id）：可按章节/块取，也可从头顺读；截断时回 next_offset。"),
    ToolDecl("search_paper_text", "search_paper_text", "read",
             "在单篇论文正文里检索：命中带块 id 与前后文——用它定位'这句话在哪一块'。"),
    ToolDecl("annotate_paper", "annotate_paper", "write",
             "在论文原文上加带位置的批注：给原句（quote）或块 id，由后端解析成精确区间再落库"
             "（段落/句子/公式/图表都能标）。回执 snippet 就是'标在哪'的证据。",
             omit=("actor",)),
    ToolDecl("update_mark", "update_mark", "write",
             "改一条批注（正文/状态/颜色）。",
             omit=("actor",)),
    ToolDecl("resolve_mark", "resolve_mark", "write",
             "把一条批注标为已解决（痕迹留着，问题算处理完）。",
             omit=("actor",)),
    ToolDecl("verify_marks", "verify_marks", "read",
             "批注回执：逐条报「重锚结果 + 命中的原句 + 前后文」——标完先看它，"
             "位置错了一眼看得出来，不必等人截图。"),
    ToolDecl("capture_paper_shot", "capture_paper_shot", "write",
             "服务端无头截图：把真实阅读页拍成 PNG 存本地并回文件路径——"
             "用 read_image 打开就能看见批注长什么样、挡没挡住正文。",
             omit=("actor",)),
    ToolDecl("read_paper_shots", "read_paper_shots", "read",
             "列出这篇论文已拍过的截图（最近优先），回本地路径交给 read_image。"),
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
    什么。根子（“允许裸 ok:false”）已回馈框架（见内部 n=3 报告，未随仓发布）。

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


#: 已切到 mecha 投影的工具（**分批切换**的当前批）：声明面由 `mecha.projection.project()`
#: 生成。名字进本表 = "本批切到投影"；未列入的仍走 `build_tool_registry` 里的手写注册。
#: ⚠ **本表只切"工具面"、不切"命令面"**：写侧那几条的 `ToolDecl` **仍在 `TOOL_DECLS` 里**
#: （命令面 `build_commands` 也遍历它 ⇒ 那是它们唯一的声明家；表内那段注释已写明），
#: 读侧那几条同理。**"已切投影"是迁移状态，不是第二份声明**。
#: ⚠ `delete_note` / `set_config_batch` 也在本列，但它们的 scope **只给人** ⇒ `project()`
#: 经 policy 判为**不投影**（返回 None）⇒ 仍不可见：
#: **"看不见"由 scope 派生，不再靠"没人把它写进工具面"**。
_PROJECTED_TOOLS: tuple[str, ...] = (
    "mark_read", "star_paper", "skip_paper", "add_note", "delete_briefing",
    "tag_paper", "tag_papers", "resolve_mark", "set_default_view", "sync_citations",
    "delete_note", "set_config_batch",
    # ---- 读侧（2026-09-30 起分批切；批 1 = `query_topics`，之后每批 7~8 条）------------
    # 读条目的声明**仍在 `TOOL_DECLS`**（`kind="read"`，一个事实一个家）；进本表只表示
    # "工具面改由 `project()` 生成"。⚠ 与写侧的关键差别：读声明的**命令声明不进 `sw.commands`**
    # （命令面是写治理面，理由与实测见 `commands.build_read_command`）。
    "query_topics",
    "read_digest", "review_status", "query_tags", "query_graph_views",
    "read_activity", "search_papers", "upstream_clusters",
    "read_paper", "related_papers", "coverage_report", "stats_timeseries",
    "watch_authors", "paper_metrics", "read_references", "read_citations",
    "query_briefings", "query_profile", "feed_generate", "read_paper_outline",
    "search_library_text", "read_paper_text", "search_paper_text", "verify_marks",
    "read_paper_shots",
)


def _params_for_model(decl, cap_params) -> dict:
    """**投影产物 + 本仓的"模型面参数口径"** ⇒ 给模型的参数 schema（在本侧合并）。

    ⚠ 为什么在本侧合并（n=1，R1）：`project()` 搬的是**命令面** schema，而
    ① "给模型的参数说明"住**能力层**（N8 单一事实源）；
    ② "对模型是否必填"是**工具面口径**（今天：全部可选 —— 命令面必填 ≠ 对模型必填）；
    这两样都是**项目口径**，框架不知道也不该知道 ⇒ 由本函数并回去（框架只搬不解释）。
    ⚠ `actor` 在这里显式去掉：投影产物自带框架的自动剔除，但**本函数覆盖了参数**
    ⇒ 被覆盖的那份由本函数负责（不是"两处表达同一件事"：框架那份已被丢弃）。
    """
    params = _derive_parameters(cap_params, decl)
    params.pop("actor", None)
    return params


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
    # ⚠ 那张表（`TOOL_DECLS`）**同时**驱动命令面与工具面：已切投影的 5 条**仍留在表里**
    # （命令面 `build_commands` 也遍历它），但**工具面**由下面 `project()` 生成 ⇒ 这里跳过它们
    # （防"两处表达同一件事"）。
    for decl in TOOL_DECLS:
        if decl.mecha_name in _PROJECTED_TOOLS:
            continue
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
    # ⭐ **已切投影的那批**（分批切换的当前批）：声明面由 `mecha.projection.project()` 生成 ——
    #   名字（惯例/改名 + 形状律）、描述（透传；有 overrides 就用它）、**scope 不被 policy 允许 ⇒ None**
    #   （"看不见"）、kind（由 side_effect **翻译**）、reversible（取能力层自己的声明 ⇒ 顺手接通
    #   "声明了没人喂"的那条链）；参数面由 `_merge_model_params` 并回本仓口径；`actor` 框架自动剔除。
    #   ⚠ 投影只给**声明面**：`execute` 必须由本侧接上（写 = 命令桥、读 = 能力桥），
    #   否则"列得出调不动"。
    from mecha.projection import ProjectionOverrides, project

    specs_by_name = {s.name: s for s in sw.commands.specs()}
    cap_rev = {c["name"]: bool(c["reversible"]) for c in capabilities.specs(container)}
    policy = getattr(sw.commands, "_scope_policy", None)
    decls_by_name = {d.mecha_name: d for d in TOOL_DECLS}
    for mecha_name in _PROJECTED_TOOLS:
        decl = decls_by_name.get(mecha_name)
        is_read = decl is not None and decl.kind == "read"
        if is_read:
            # ⭐ **读侧**（2026-09-30）：声明仍在**同一张** `TOOL_DECLS`（一个事实一个家），
            #   但它的**命令声明不进命令面**——命令面是写治理面，不变式是"每一条都是写命令"
            #   （要通道 / 要 reason / 要非零预估，两条冻结判据逐条钉着），而读命令进那个面
            #   **只为投影**、却会让那两条不变式变成假的。理由与实测见 `commands.build_read_command`。
            # 延迟导入：commands 依赖 tools（避免模块级循环）。
            from .commands import build_read_command

            projected = project(
                build_read_command(container, decl),
                execute=_make_capability_bridge(container, decl.cap_name),
                policy=policy, side="ai",
                overrides=ProjectionOverrides(reversible=cap_rev.get(decl.cap_name, False)),
            )
        else:
            spec = specs_by_name.get(mecha_name)
            if spec is None:
                raise MechaError(f"投影清单里的命令 {mecha_name!r} 不在命令面上",
                                 kind="capability_missing",
                                 hint="补命令注册，或把它从 _PROJECTED_TOOLS 里去掉")
            projected = project(
                spec,
                execute=_make_command_bridge(sw.commands, sw.gate, channel, mecha_name,
                                             approval=sw.approval),
                policy=policy, side="ai",
                overrides=ProjectionOverrides(reversible=cap_rev.get(mecha_name, False)),
            )
        if projected is None:                 # policy 不让 ⇒ 按设计"看不见"
            continue
        # ⚠ **参数面必须在上面那句 `continue` 之后算**（沿用重构前的次序）：`delete_note` /
        #   `set_config_batch` **不是能力**（它们是人类专属命令）⇒ 它们的 scope 只给人、
        #   投影返回 None、根本走不到这里；若把取参提到前面，那两条会让装配期直接炸
        #   （`_cap_params` fail loud）。这也是"看不见由 scope 派生"的连带前提。
        #   ⚠ 读侧用**能力层口径**（`_derive_parameters`），不用写侧那句 `params.pop("actor")`：
        #   `read_activity` 的 `actor` 是**过滤条件**（保留给模型），不是归因（见本模块 docstring）。
        if is_read:
            parameters = _derive_parameters(_cap_params(container, decl.cap_name), decl)
        else:
            parameters = _params_for_model(
                ToolDecl(mecha_name, mecha_name, "write", "", omit=()),
                _cap_params(container, mecha_name))
        reg.register(define_tool(
            name=projected.name,
            description=projected.description,
            parameters=parameters,
            output_schema={"type": "object", "required": ["ok"]},
            execute=projected.execute,
            banned_words=_BANNED_WORDS,
        ))
    _register_config_tools(reg, sw, channel)
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


def _register_config_tools(reg: ToolRegistry, sw, channel: Channel) -> None:
    """配置态工具（Scheme E：标量走 Gate）：**set_config 走命令面**，read_config 直读快照。

    ⭐ **写的那半走命令面**（2026-09-26）：`set_config` 原先是**唯一直写 `gate.set` 的写工具**
    ⇒ 框架账上只有"某个配置键变了"这条**键事件**，没有"谁执行了一次 set_config"这条**操作审计**。
    现在它经 `invoke_command`（与 19 条写能力同一条路）⇒ **键事件与 `command.set_config` 审计
    同时落账**（判据 `test_set_config_via_tools_leaves_command_audit`）。
    ⚠ **只读的那半（`read_config`）一行不动**：D-4 —— 只读不经门、也不产生操作审计
    （对偶判据 `test_read_config_stays_ungated`）。
    ⚠ **工具签名保持具名**（`key, value, reason="ai set_config"`，不用 `**kwargs`）：该工具的 MCP
    投影历史上依赖"签名可用"（MCP 的 required 从签名派生）⇒ 换形状会改掉模型看到的参数面。
    """
    from .commands import invoke_command  # 延迟导入：commands 依赖 tools，避免模块级循环

    gate = sw.gate  # 只读那半仍直读快照（不经门）

    def _set_config_via_command(key: str, value, reason: str = "ai set_config"):
        """工具面 → 命令面。成功把命令体 values 原样返回（**回执形状不变**：
        `ok/key/before/resolved/readback`）；失败按命令面桥的同一做法抛 `MechaError`
        （kind/hint/suggest 取自 failure）⇒ 对模型的失败档位与从前一致。"""
        res = invoke_command(sw.commands, sw.gate, channel, "set_config",
                             {"key": key, "value": value, "reason": reason},
                             approval=sw.approval)
        if res["is_error"]:
            failure = res["error"]
            info = failure.get("info", {})
            raise MechaError(
                str(failure.get("message") or "配置写入失败"),
                kind=str(info.get("kind") or "error"),
                hint=str(info.get("hint") or ""),
                suggest=str(info.get("suggest") or ""),
            )
        return res["value"]

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
        execute=_set_config_via_command,
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
    # ⚠ 对齐 mecha 新事件形状（2026-09-30）：事件的 `value` 改名 `after`。
    return {"ok": True, "key": key, "before": before,
            "resolved": ev.after, "readback": gate.snapshot.get(key)}


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
            "how_to_open": ("写权默认已是 open（两侧都能写，2026-09-26 起）。"
                            "若被切成了 ai 独占/human 独占/locked：在 Web 的 /settings 「写权模式」"
                            "卡点「放开（open）」（需控制口令）；也可用 "
                            "`paperpilot serve/ai --mode open` 起步。AI 侧不能自解锁。")}


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

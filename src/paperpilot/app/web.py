"""FastAPI Web 应用：本地面板（今日简报 / 论文库 / 详情 / 设置）。

不依赖任何 CDN（离线可用）；交互全部用原生表单 POST + 303 重定向。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..capabilities import registry_for
from ..config import TopicCfg, save_settings
from .container import Container, run_in_background

#: 域事件 ↔ 框架账 的**互引键**：框架事件的 `after` 里带这些实体标识，域事件的 `target` 是同一个标识。
#: ⚠ 域事件**没有** `call_id`（`repo._event` 的字段里就没有它）⇒ 互引只能**按实体标识**尽力而为：
#: 匹配不上是**正常**的（读操作 / 被门拒 / 启动痕 / YAML 键都没有命令审计）。
_AUDIT_REF_KEYS = ("arxiv_id", "note_id", "topic", "added", "date", "run_id",
                   "briefing_id", "path", "undone_seq", "seq")


def link_audit_to_targets(audit: list[dict]) -> dict[str, list[str]]:
    """框架账 → ``{域 target: [命令键, …]}``：把两册**对上号**（对不上的就不出现在结果里）。

    ⚠ **对齐 mecha 新事件形状（2026-09-30）**：账事件是
    ``seq/kind/op/target/after/before/actor/reason/call_id/ts/undoable``
    ⇒ 命令名在 **`op`**（`command.<name>`），实体值在 **`after`**（命令审计里落在 `after.result_ref`）。
    为什么单独一个纯函数：页面渲染难断言，而"**能对上号**"是给用户的核心价值
    ⇒ 把它做成可直测的映射（判据 `test_activity_links_domain_events_to_framework_audit`）。
    """
    linked: dict[str, list[str]] = {}
    for rec in audit:
        payload = rec.get("after")
        if not isinstance(payload, dict):
            continue
        ref = payload.get("result_ref") if isinstance(payload.get("result_ref"), dict) else {}
        for key in _AUDIT_REF_KEYS:
            got = payload.get(key, ref.get(key))
            if isinstance(got, (str, int)) and str(got):
                linked.setdefault(str(got), []).append(str(rec.get("op") or ""))
                break
    return linked


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def create_app(container: Container, stack: dict | None = None) -> FastAPI:
    """建 FastAPI 应用。

    ``stack`` = mecha 共享栈（由统一启动入口 ``paperpilot serve`` 传入）。给了它，
    人类面的论文库写就走**同一道门**（human 通道 + authority + 审计进 cockpit）；
    不给（如独立测试）则回退到直调 retrieval/capabilities（向后兼容）。
    """
    app = FastAPI(title="PaperPilot", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["prettyjson"] = _pretty_json
    app.state.container = container
    app.state.stack = stack
    app.state.templates = templates

    def render(request: Request, template: str, **ctx) -> HTMLResponse:
        ctx.setdefault("request", request)
        ctx.setdefault("container", container)
        ctx.setdefault("msg", request.query_params.get("msg", ""))
        return templates.TemplateResponse(request, template, ctx)

    def _authority_mode() -> str | None:
        """当前写权模式（无栈 ⇒ None）：设置页那张「写权模式」卡用它显示现状。"""
        if stack is None:
            return None
        return str(getattr(stack["authority"].mode, "value", stack["authority"].mode))

    def _gated(cmd: str, **args) -> str:
        """经门（human 通道）写一个命令；返回给用户的消息串（空串=成功无提示）。"""
        from ..mecha_adapter.hub import human_write

        res = human_write(stack, cmd, **args)
        if res.get("is_error"):
            info = res["error"].get("info", {})
            return f"操作被拒（{info.get('kind', 'error')}）：{res['error'].get('message', '')}"
        return ""

    # ---------------------------------------------------------------- 简报
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        date_str = _today()
        briefing = container.retrieval.briefing(date_str)
        dates = [b.date for b in container.retrieval.recent_briefings(limit=14)]
        return render(
            request,
            "digest.html",
            briefing=briefing,
            date=date_str,
            recent_dates=dates,
            is_today=True,
        )

    @app.get("/digest/{date}", response_class=HTMLResponse)
    def digest(request: Request, date: str):
        briefing = container.retrieval.briefing(date)
        dates = [b.date for b in container.retrieval.recent_briefings(limit=14)]
        return render(
            request,
            "digest.html",
            briefing=briefing,
            date=date,
            recent_dates=dates,
            is_today=(date == _today()),
        )

    # ---------------------------------------------------------------- 论文库
    @app.get("/papers", response_class=HTMLResponse)
    def papers(
        request: Request,
        q: str = "",
        label: str = "",
        category: str = "",
    ):
        results = container.retrieval.search(
            q, label=label or None, primary_category=category or None, limit=100
        )
        return render(
            request, "papers.html", results=results, q=q, label=label, category=category
        )

    @app.get("/papers/{arxiv_id}", response_class=HTMLResponse)
    def paper_detail(request: Request, arxiv_id: str):
        detail = container.retrieval.detail(arxiv_id)
        if detail is None:
            return render(request, "paper_detail.html", detail=None, arxiv_id=arxiv_id)
        # M1 埋点：详情页打开=view 信号（弱正 +0.3）；记账失败不拦页面
        try:
            container.repo.record_signal(arxiv_id, "view", actor="human",
                                         reason="Web 详情页浏览")
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("paperpilot.web").exception("view 信号记账失败（页面照常）")
        return render(request, "paper_detail.html", detail=detail, arxiv_id=arxiv_id,
                      paper_html=container.repo.get_paper_html(arxiv_id),
                      mark_count=len(container.repo.marks_for(arxiv_id)))

    # ---------------------------------------------------------------- 推荐流面板（期票模型：只读最新期，不现场重算）
    _LANE_LABEL = {"primary": "主兴趣", "adjacent": "邻接", "hot": "热点", "explore": "探索"}

    def _enrich_feed_items(items: list[dict]) -> list[dict]:
        """期快照只钉“哪些篇/序/为什么”；中文摘要与评审理由渲染时现 join（跟最新走）。"""
        out = []
        for e in items:
            d = container.retrieval.detail(e["arxiv_id"])
            s = d["summary"] if d else None
            sc = max(d["scores"], key=lambda x: x.id, default=None) if d else None
            out.append({**e,
                "lane_label": _LANE_LABEL.get(e["lane"], e["lane"]),
                "summary": ({"tldr": s.tldr, "problem": s.problem, "method": s.method,
                             "results": s.results, "novelty": s.novelty} if s else None),
                "ai_summary": bool(s and s.model and s.model != "heuristic"),
                "review_reason": (sc.reason if sc and sc.reason else "")})
        return out

    @app.get("/feed", response_class=HTMLResponse)
    def feed_page(request: Request, preview: int = 0, limit: int = 25,
                  mix: str = "auto", days: int = 14, offset: int = 0,
                  seen_days: int = 7):
        """默认只读最新一期（AI 经 publish_feed 发布）；`preview=1` 才现场重算且**不落库不覆盖期**。
        面板无刷新/换页按钮——驱动权全在 dsh 对话里的 AI。"""
        if preview:
            res = registry_for(container).invoke("feed_generate", limit=limit, mix=mix,
                                                 days=days, offset=offset,
                                                 seen_days=seen_days)
            if not res.get("ok"):
                return render(request, "feed.html", entries=[], count=0, preview=True,
                              issue_info=None,
                              msg=res.get("error", {}).get("message", "预览生成失败"))
            return render(request, "feed.html",
                          entries=_enrich_feed_items(res.get("feed", [])),
                          count=res.get("count", 0), preview=True, issue_info=None,
                          msg="临时预览（现场重算，不落库；面板刷新后仍回到最新一期）")
        issue = container.repo.latest_feed_issue()
        if issue is None:
            return render(request, "feed.html", entries=[], count=0, preview=False,
                          issue_info=None,
                          msg="还没有 feed 期：在 dsh 说一句「刷 20 条推荐」让 AI publish_feed")
        p = issue.params or {}
        return render(request, "feed.html",
                      entries=_enrich_feed_items(list(issue.items or [])),
                      count=len(issue.items or []), preview=False,
                      issue_info={"id": issue.id, "ts": str(issue.ts)[:16],
                                  "actor": issue.actor, "reason": issue.reason,
                                  "params": p},
                      msg="")

    @app.post("/papers/{arxiv_id}/uninterested")
    def uninterested(arxiv_id: str, next_url: str = Form("")):
        """显式负反馈（最强负权）：直写 repo 信号（轻量人类操作，同 M0 族）。"""
        try:
            container.repo.record_signal(arxiv_id, "uninterested", actor="human",
                                         reason="Web 显式不感兴趣")
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("paperpilot.web").exception("uninterested 信号记账失败")
        dest = next_url if next_url.startswith("/") else _back(arxiv_id)   # 只允站内路径，防开放重定向
        return RedirectResponse(dest, status_code=303)

    # ---------------------------------------------------------------- 图底座实例（视图一等公民；改图的手在 AI）
    def _effective_spec(query: dict) -> dict:
        """视图解析次序（总纲：显式意图 > 视图 > 配置）：
        query 参数 > ?view= 指定视图 > **默认视图**（AI 发布的）> GraphCfg 缺省。"""
        from . import graph_view as gv
        base = {k: getattr(container.settings.graph, k, None)
                for k in ("color_by", "label_mode", "badge", "arrow_size", "max_nodes",
                          "max_edges", "group_by", "group_quota", "sort_within", "size_by",
                          "layer_gap", "node_gap", "max_label_len", "focus_depth",
                          "sides", "in_lib_only", "layout")
                if hasattr(container.settings.graph, k)}
        base = {k: v for k, v in base.items() if v is not None}
        base["depth"] = container.settings.graph.focus_depth
        base["label_max"] = container.settings.graph.max_label_len
        want = str((query or {}).get("view") or "").strip()
        row = container.repo.get_graph_view(want) if want else container.repo.default_graph_view()
        spec = {**base, **(row["spec"] if row else {})}
        if not want:
            spec.setdefault("root", container.settings.graph.root_default)
        return gv.spec_from_query(query or {}, spec)

    @app.get("/network", response_class=HTMLResponse)
    def network(request: Request, focus: str = "", root: str = "", depth: int = 0,
                view: str = ""):
        """引文网络 = 图底座实例，渲染**默认视图**（AI 经 set_graph_view 发布的那张）。

        不带任何参数打开本页 = 我编排好的那张图；`?view=` 切别的已发布视图；
        `?root=&depth=` 是临时覆盖（显式意图优先）。层号＝从根的拓扑深度；
        `?layout=timeline` 改年代编排；着色/分组/标签/预算都在视图里。
        """
        from . import graph_view as gv
        q = dict(request.query_params)
        spec = _effective_spec(q)
        if root:
            spec["root"] = root.strip()
        if depth:
            spec["depth"] = int(depth)
        if view:
            spec["view"] = view
        payload = gv.build(container.repo, container.retrieval, container.settings, spec,
                           focus=focus)
        stats = payload["stats"]
        msg = request.query_params.get("msg", "")
        if payload.get("empty"):
            msg = ("引文图谱还空着：这页是 AI 调研成果的显示器——"
                   "在 dsh 让 AI 对关键论文 sync_citations，调查完回这里看图")
        elif not payload.get("root_found", True):
            msg = (f"根节点 {spec['root']} 在图里没有边：先让 AI 对它 sync_citations"
                   f"（要下游层再加 sync_cited_by）")
        node_dicts = [{k: v for k, v in n.items() if k != "card"} for n in payload["nodes"]]
        return render(request, "network.html", nodes=payload["nodes"], links=payload["links"],
                      bands=payload["bands"], columns=payload["columns"],
                      sides=payload.get("sides", []),
                      legend=payload["legend"], views=payload["views"],
                      spec=payload["spec"], view_title=payload["spec"].get("title", ""),
                      focus=focus, msg=msg, root=payload["root"],
                      depth=payload["depth"], label_on=payload["label_on"],
                      layout=payload["layout"], badge_on=payload["badge_on"],
                      arrow_size=payload["arrow_size"], color_by=payload["color_by"],
                      group_by=payload["group_by"], svg_w=payload["width"],
                      svg_h=payload["height"], stats=stats, node_dicts=node_dicts)

    @app.get("/network.json")
    def network_json(request: Request, focus: str = "", root: str = "", depth: int = 0,
                     view: str = ""):
        """**渲染回执**：同一份视图编译出的几何/配色/标签决策（确定性可复算）。

        给 AI 自查用——"箭头被节点盖住""标签没出来""配色没生效"这类问题
        以前只能靠用户截图发现，现在交付前就能核对（layout/坐标/端点/颜色/标签模式）。
        """
        from fastapi.responses import JSONResponse

        from . import graph_view as gv
        q = dict(request.query_params)
        spec = _effective_spec(q)
        if root:
            spec["root"] = root.strip()
        if depth:
            spec["depth"] = int(depth)
        payload = gv.build(container.repo, container.retrieval, container.settings, spec,
                           focus=focus)
        return JSONResponse({
            "view": view or "default", "spec": payload["spec"], "stats": payload["stats"],
            "width": payload["width"], "height": payload["height"],
            "label_on": payload["label_on"], "arrow_size": payload["arrow_size"],
            "sides": payload.get("sides", []),          # 上游/下游侧标注（自查用）
            "bands": payload["bands"], "columns": payload["columns"],
            "legend": payload["legend"],
            "nodes": [{k: v for k, v in n.items() if k != "card"}
                      for n in payload["nodes"]],
            "links": payload["links"],
        })

    @app.get("/graph/go/{arxiv_id}")
    def graph_node_go(arxiv_id: str):
        """F3 节点句柄：点击=站内闭环——不在库先 fetch_paper_by_id（走命令面、留痕），
        再进本站论文面。**永不外跳**（出站点只在详情页的原文/PDF，那是有意漏斗）。"""
        if container.repo.get_paper(arxiv_id) is None:
            if stack is None:
                res = registry_for(container).invoke(
                    "fetch_paper_by_id", arxiv_id=arxiv_id, actor="human",
                    reason=f"图节点点击入库 {arxiv_id}")
            else:
                from ..mecha_adapter.hub import human_write
                r = human_write(stack, "fetch_paper_by_id", arxiv_id=arxiv_id,
                                reason=f"图节点点击入库 {arxiv_id}")
                res = ({"ok": True} if not r.get("is_error")
                       else {"ok": False,
                             "error": {"message": r.get("error", {}).get("message", "")}})
            if not res.get("ok"):
                return RedirectResponse(
                    _with_msg("/network",
                              f"入库失败：{res.get('error', {}).get('message', '未知原因')}"
                              "——已回图谱，节点没丢，可再点重试"), status_code=303)
        return RedirectResponse(f"/papers/{arxiv_id}", status_code=303)

    # ---------------------------------------------------------------- 精读（HTML 正文 + 批注）
    # 定位：界面② 的"精读现场"。正文由**本站同源**提供（不直嵌 arXiv），三个理由：
    # ① 跨域 iframe 会让 canvas 变脏 ⇒ 无头截图与 DOM 标注都做不了；
    # ② 我们能剥脚本（外部内容按敌意内容处理）；③ 资源已离线，断网也能读。
    def _paper_html_base(arxiv_id: str):
        meta = container.repo.get_paper_html(arxiv_id)
        if not meta or meta.get("status") != "ok":
            return None, meta
        base = (Path(container.settings.data_dir) / "paper_html" / arxiv_id
                / f"v{int(meta.get('version') or 0)}")
        return (base if (base / "index.html").exists() else None), meta

    @app.get("/paper/{arxiv_id}/html", response_class=HTMLResponse)
    def paper_html(arxiv_id: str):
        """改写并离线化之后的正文本身（同源提供 ⇒ 父页面可以标注它、也可以截图）。"""
        base, _meta = _paper_html_base(arxiv_id)
        if base is None:
            return HTMLResponse(
                "<p style='font:15px system-ui;padding:24px;color:#66708a'>"
                "这篇论文还没归档 HTML 正文（arXiv 未提供 HTML 的论文不进精读体系）。</p>",
                status_code=404)
        return HTMLResponse((base / "index.html").read_text(encoding="utf-8"))

    @app.get("/paper/{arxiv_id}/assets/{name}")
    def paper_asset(arxiv_id: str, name: str):
        """离线资源（CSS/图片）。只认归档目录里的文件——不做任意路径读取（防穿越）。"""
        from fastapi.responses import FileResponse

        base, _meta = _paper_html_base(arxiv_id)
        if base is None:
            return HTMLResponse("没有归档正文", status_code=404)
        assets = (base / "assets").resolve()
        target = (assets / name).resolve()
        if assets != target.parent or not target.is_file():
            return HTMLResponse("没有这个资源", status_code=404)
        return FileResponse(target)

    @app.get("/read/{arxiv_id}", response_class=HTMLResponse)
    def read_paper(request: Request, arxiv_id: str, focus: int = 0, shot: int = 0):
        """精读页：正文（同源 iframe）+ 批注层 + 侧栏。AI 的批注经轮询实时上屏。"""
        meta = container.repo.get_paper_html(arxiv_id)
        p = container.repo.get_paper(arxiv_id)
        return render(request, "read.html", arxiv_id=arxiv_id, meta=meta,
                      paper_title=(p.title if p is not None else ""),
                      marks=container.repo.marks_for(arxiv_id),
                      focus=int(focus or 0), shot=int(shot or 0))

    @app.get("/read/{arxiv_id}/marks.json")
    def read_marks(arxiv_id: str, since_id: int = 0):
        """批注增量接口：页面每 4 秒问一次 ⇒ AI 落一条就上一次屏（"实时"靠它兑现）。"""
        from fastapi.responses import JSONResponse

        marks = container.repo.marks_for(arxiv_id, since_id=int(since_id or 0))
        return JSONResponse({"marks": marks, "count": len(marks)})

    @app.post("/read/{arxiv_id}/marks/{mark_id}/delete")
    def delete_mark_row(arxiv_id: str, mark_id: int):
        """**删批注（人类专属）**：AI 工具面里没有这一项——精读痕迹的处置权归人。

        与 `/settings/briefings/delete` 同款：有栈走命令面（写权门 + 审计），无栈回退能力层。
        """
        from urllib.parse import quote as _q

        if stack is None:
            res = registry_for(container).invoke(
                "delete_mark", mark_id=int(mark_id), actor="human", reason="阅读页删批注")
            msg = ("已删除该批注（可 undo 撤销）" if res.get("ok")
                   else f"删除失败：{res.get('error', {}).get('message', '未知错误')}")
            return RedirectResponse(f"/read/{arxiv_id}?msg={_q(msg)}", status_code=303)
        gate_msg = _gated("delete_mark", mark_id=int(mark_id), reason="阅读页删批注")
        msg = gate_msg or "已删除该批注（/activity 可 undo 撤销）"
        return RedirectResponse(f"/read/{arxiv_id}?msg={_q(msg)}", status_code=303)

    @app.get("/lab", response_class=HTMLResponse)
    def lab(request: Request):
        """调研仪表盘：覆盖率 + 缺卡工单 + 30 天趋势/漏斗/AI 成本（纯展示，数字全复用 M4）。"""
        cov = container.repo.coverage_report(sample_missing=12)
        st = container.repo.stats_timeseries(days=30)
        dn = st["daily_new"]
        mx = max(dn.values()) if dn else 1
        bars = [{"d": k, "n": v, "pct": max(2, round(100 * v / mx))}
                for k, v in sorted(dn.items())][-14:]
        return render(request, "lab.html", cov=cov, st=st, bars=bars, msg="")

    @app.post("/settings/graph")
    def save_graph(max_label_len: int = Form(18), layer_gap: int = Form(130),
                   node_gap: int = Form(90), max_nodes: int = Form(90),
                   max_edges: int = Form(400), focus_depth: int = Form(2),
                   size_by: str = Form("degree"), color_by: str = Form("auto"),
                   sort_within: str = Form("weight"), root_default: str = Form(""),
                   layout: str = Form("layer"), group_by: str = Form("none"),
                   group_quota: int = Form(0), label_mode: str = Form("auto"),
                   label_style: str = Form("title"), label_auto_max: int = Form(60),
                   badge: str = Form("on"), arrow_size: int = Form(13)):
        """图**缺省**呈现参数（视图未覆盖时生效）：纯展示项，不进 gate schema，YAML 即真相。

        AI 侧发视图走 ``set_graph_view``（落 graph_views 表、即时生效）；本表单管"没有视图时"
        的缺省，两条路互不覆盖。
        """
        from ..config import save_settings
        s = container.settings
        try:
            s.graph.max_label_len = max(6, int(max_label_len))
            s.graph.layer_gap = max(40, int(layer_gap))
            s.graph.node_gap = max(24, int(node_gap))
            s.graph.max_nodes = max(8, int(max_nodes))
            s.graph.max_edges = max(20, int(max_edges))
            s.graph.focus_depth = min(6, max(1, int(focus_depth)))
            s.graph.group_quota = max(0, min(50, int(group_quota)))
            s.graph.label_auto_max = max(0, min(400, int(label_auto_max)))
            s.graph.arrow_size = max(6, min(40, int(arrow_size)))
        except (TypeError, ValueError):
            return RedirectResponse(_with_msg("/settings", "图参数需为整数"), status_code=303)
        s.graph.size_by = size_by if size_by in ("degree", "weight", "flat") else "degree"
        s.graph.color_by = (color_by if color_by in ("auto", "kind", "in_lib", "weight",
                                                     "tag", "group") else "auto")
        s.graph.sort_within = sort_within if sort_within in ("weight", "year") else "weight"
        s.graph.root_default = (root_default or "").strip()
        s.graph.layout = layout if layout in ("layer", "timeline") else "layer"
        s.graph.group_by = group_by if group_by in ("none", "tag", "group") else "none"
        s.graph.label_mode = label_mode if label_mode in ("auto", "always", "hover") else "auto"
        s.graph.label_style = label_style if label_style in ("title", "id") else "title"
        s.graph.badge = str(badge).lower() not in ("0", "off", "false", "no")
        save_settings(s)
        return RedirectResponse(_with_msg("/settings", "图缺省已存（视图优先；层数不在内：那是拓扑）"),
                                status_code=303)

    # ---------------------------------------------------------------- 论文动作
    # 有 stack（统一启动）→ 经 mecha 命令面（human 通道 + 写权门 + 审计）；否则直调（向后兼容）。
    @app.post("/papers/{arxiv_id}/read")
    def toggle_read(arxiv_id: str):
        if stack is None:
            container.retrieval.toggle_read(arxiv_id)
            return RedirectResponse(_back(arxiv_id), status_code=303)
        detail = container.retrieval.detail(arxiv_id)
        cur = bool(detail and detail.get("reading") and detail["reading"].read)
        msg = _gated("mark_read", arxiv_id=arxiv_id, read=not cur, reason="Web 切换已读态")
        return RedirectResponse(_back(arxiv_id, msg), status_code=303)

    @app.post("/papers/{arxiv_id}/star")
    def toggle_star(arxiv_id: str):
        if stack is None:
            container.retrieval.star(arxiv_id)
            return RedirectResponse(_back(arxiv_id), status_code=303)
        msg = _gated("star_paper", arxiv_id=arxiv_id, reason="Web 收藏/取消收藏")
        return RedirectResponse(_back(arxiv_id, msg), status_code=303)

    @app.post("/papers/{arxiv_id}/skip")
    def mark_skip(arxiv_id: str):
        if stack is None:
            container.retrieval.skip(arxiv_id)
            return RedirectResponse(_back(arxiv_id), status_code=303)
        msg = _gated("skip_paper", arxiv_id=arxiv_id, reason="Web 标记不感兴趣")
        return RedirectResponse(_back(arxiv_id, msg), status_code=303)

    @app.post("/papers/{arxiv_id}/note")
    def add_note(arxiv_id: str, content: str = Form(...)):
        if stack is None:
            container.retrieval.add_note(arxiv_id, content)
            return RedirectResponse(_back(arxiv_id), status_code=303)
        msg = _gated("add_note", arxiv_id=arxiv_id, content=content, reason="Web 添加笔记")
        return RedirectResponse(_back(arxiv_id, msg), status_code=303)

    @app.post("/notes/{note_id}/delete")
    def delete_note(note_id: int, arxiv_id: str = Form(...)):
        """删笔记：**人类独有的管理动作**（不与 AI 争写）⇒ 命令面有它、**AI 工具面没有它**。

        从前的病与 `reset_profile` 同款：**没有任何入口走门**（Web 直调 retrieval）⇒ 域里有
        `delete_note` 事件、框架账里没有任何操作审计。现在有栈时走命令面（写权 + `command.delete_note`）；
        **无栈**（单测/独立部署）才回退直调——与 `/settings/briefings/delete` 同款。
        """
        if stack is None:
            container.retrieval.delete_note(note_id)
            return RedirectResponse(_back(arxiv_id), status_code=303)
        msg = _gated("delete_note", note_id=int(note_id), reason="Web 面板删笔记")
        return RedirectResponse(_back(arxiv_id, msg), status_code=303)

    # ------------------------------------ 出站跳转（M0：本地 PDF 退役，下载=浏览器直下；跳转顺手记信号）
    # 漏斗三层可测：view（详情页）→ outbound（经我方跳 arXiv abs）→ download（经我方直下 PDF）。
    # 信号与 delete_note 同族：直写 repo、不经命令面（不与 AI 争写），但进事件总线留痕；M1 画像消费。
    def _signal_then_redirect(arxiv_id: str, signal: str, url: str) -> RedirectResponse:
        try:
            container.repo.record_signal(arxiv_id, signal, source="measured",
                                         actor="human", reason=f"Web {signal} 跳转")
        except Exception:  # noqa: BLE001 — 记账失败绝不拦用户的跳转，但要留日志可查
            import logging
            logging.getLogger("paperpilot.web").exception("record_signal 失败，跳转照常")
        return RedirectResponse(url)

    @app.get("/papers/{arxiv_id}/go")
    def go_arxiv(arxiv_id: str):
        """去 arXiv abs 页（outbound 信号 + 302）。"""
        detail = container.retrieval.detail(arxiv_id)
        url = (detail["paper"].abs_url if detail else "") or f"https://arxiv.org/abs/{arxiv_id}"
        return _signal_then_redirect(arxiv_id, "outbound", url)

    @app.get("/papers/{arxiv_id}/pdf")
    def download_pdf(arxiv_id: str):
        """直下 PDF（download 信号 + 302 到 arXiv；文件不落库，无本地缓存）。"""
        detail = container.retrieval.detail(arxiv_id)
        url = (detail["paper"].pdf_url if detail else "") or f"https://arxiv.org/pdf/{arxiv_id}"
        return _signal_then_redirect(arxiv_id, "download", url)

    # ---------------------------------------------------------------- 设置
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        """人类控制面：运行状态 / **写权模式（含控制口令）** / 全局参数 / 主题。

        ⚠ 写权切换这张卡原在 `/monitor` 页上；**面板视图退役后搬到这里**
        （AI 监控改走 dsh 原生 tab）。**控制端点 `POST /monitor/mode` 的路径与鉴权未动**——
        只换了它的 UI 落点与重定向目标（不这样搬，"删掉面板页"就会顺手把人类切写权的
        唯一入口也删掉：口令输入框只在那个页面上）。
        """
        from .control_token import read_control_token

        s = container.settings
        views = container.repo.list_graph_views()
        return render(
            request,
            "settings.html",
            settings=s,
            last_run=container.repo.last_run(),
            counts=container.repo.counts_by_status(),
            briefings=container.repo.briefings(limit=30),
            graph_views=[{"name": v["name"], "is_default": v["is_default"],
                          "title": (v["spec"] or {}).get("title", ""),
                          "mode": (v["spec"] or {}).get("mode", "auto"),
                          "named": len((v["spec"] or {}).get("layers") or {}),
                          "ts": v["ts"], "actor": v["actor"]} for v in views],
            mode=_authority_mode(),
            control_ready=read_control_token() is not None,
        )

    @app.post("/settings/topics")
    def save_topic(
        request: Request,
        action: str = Form(...),
        index: str = Form(""),
        name: str = Form(""),
        description: str = Form(""),
        keywords: str = Form(""),
        exclude_keywords: str = Form(""),
        categories: str = Form(""),
        authors: str = Form(""),
        weight: float = Form(0.5),
    ):
        """主题 = **词条包**：只保留"新建 + 删除"（2026-09-30 用户裁决：逐字段规则编辑已过时）。

        每次增删都调 `sync_topic_pool` **幂等重建注入**：新建 ⇒ 按 weight 注入画像池；
        删除 ⇒ 按 `source` 精确撤掉它注入的基线（行为学到的权重保留）。
        """
        s = container.settings
        topics = list(s.topics)
        if action == "add":
            if not (keywords.strip() or categories.strip() or authors.strip()):
                return RedirectResponse(
                    "/settings?msg=空主题包没用：至少给关键词/作者/分类之一", status_code=303)
            topics.append(_topic_from_form(name, description, keywords, exclude_keywords,
                                           categories, authors, weight))
            msg = f"已新建主题包「{name}」并注入画像池"
        elif action == "delete" and index.isdigit() and 0 <= int(index) < len(topics):
            removed = topics.pop(int(index))
            msg = f"已删除主题包「{removed.name}」，其注入权重已撤"
        else:
            return RedirectResponse("/settings?msg=无效操作（只支持新建/删除）", status_code=303)
        s.topics = topics
        save_settings(s)
        container.repo.sync_topics(
            topics, actor="human",
            reason=f"Web 设置页主题包操作（{action}）「{name}」",
        )
        pool = container.repo.sync_topic_pool(
            topics, actor="human",
            reason=f"Web 设置页主题包操作（{action}）「{name}」注入画像池",
        )
        return RedirectResponse(
            f"/settings?msg={msg}（注入 {pool['injected']} · 撤权 {pool['released']}）",
            status_code=303)

    @app.post("/settings/briefings/delete")
    def delete_briefing_row(date: str = Form(...)):
        """删简报：与主题不同——它属论文库写，走**命面**（human 通道 + 写权门
        + 审计），与论文写同构。无栈（单测/独立部署）时回退直调。
        可撤销：`repo.delete_briefing` 存了 markdown+stats 快照，`_restore` 会重建行。"""
        from urllib.parse import quote

        if stack is None:
            result = registry_for(container).invoke(
                "delete_briefing", date=date, actor="human",
                reason="Web 设置页删简报",
            )
            if result.get("ok"):
                msg = f"已删除 {date} 的简报（可 undo 撤销）"
            else:
                e = result.get("error") or {}
                msg = f"删除失败：{e.get('message', '未知错误')}"
            return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)
        gate_msg = _gated("delete_briefing", date=date, reason="Web 设置页删简报")
        msg = gate_msg or f"已删除 {date} 的简报（/activity 可 undo，或让 AI 调 undo_change(seq=0)）"
        return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)

    @app.post("/settings/views/default")
    def set_default_view_row(name: str = Form(...)):
        """切默认视图（人侧入口）：走命令面（写权门 + 审计）。无栈时回退直调能力层。

        与 `/settings/briefings/delete` 同款——人侧的"处置"也要留痕，不直写数据库。
        """
        from urllib.parse import quote

        if stack is None:
            result = registry_for(container).invoke(
                "set_default_view", name=name, actor="human",
                reason="Web 设置页切默认视图")
            msg = (f"默认视图已切到「{name}」" if result.get("ok")
                   else f"切换失败：{(result.get('error') or {}).get('message', '未知错误')}")
            return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)
        gate_msg = _gated("set_default_view", name=name, reason="Web 设置页切默认视图")
        msg = gate_msg or f"默认视图已切到「{name}」——刷新 /network 即是这张"
        return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)

    @app.post("/settings/views/delete")
    def delete_view_row(name: str = Form(...)):
        """**删除视图（人类专属）**：AI 侧没有这个工具——画出来的图是"作品"，处置权归人。

        数据层留了 spec 快照 ⇒ 删错了可在 `/activity` undo（或让 AI `undo_change(seq=0)`）。
        """
        from urllib.parse import quote

        if stack is None:
            result = registry_for(container).invoke(
                "delete_graph_view", name=name, actor="human",
                reason="Web 设置页删视图")
            msg = (f"已删除视图「{name}」（可 undo 撤销）" if result.get("ok")
                   else f"删除失败：{(result.get('error') or {}).get('message', '未知错误')}")
            return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)
        gate_msg = _gated("delete_graph_view", name=name, reason="Web 设置页删视图")
        msg = gate_msg or f"已删除视图「{name}」（/activity 可 undo 撤销）"
        return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)

    @app.post("/settings/general")
    def save_general(
        lookback_days: int = Form(7),
        threshold: float = Form(0.6),
        quota_per_topic: int = Form(4),
        max_papers: int = Form(12),
        max_per_author: int = Form(1),
        must_read_cap: int = Form(3),
        review_floor: float = Form(0.4),
        notify_enabled: bool = Form(False),
        webhook_url: str = Form(""),
    ):
        """保存全局参数——⚠ **分两块，两块的性质不一样**（第 5 件修"两个真相源"）：

        * **6 个标量**（`lookback_days` + 5 个 `scoring.*`）**在 `CONFIG_SCHEMA` 里**
          ⇒ 它们是 **Gate 状态**（seed 进快照、`gate_scoring_override` 让流水线以**快照**为准）
          ⇒ 从前这里直写 YAML+内存 = **绕过了它们自己的权威**（面板 `/config` 看快照、
          Web 改 YAML ⇒ 同一个键两个真相源）。现在走**批量命令** `set_config_batch`
          （内部 `gate.set_batch`，**整批原子**：任一键非法 ⇒ 一个都不落地）+ `command.*` 审计；
          随后写 YAML 只是**留启动种子**（`seed` 的语义），**运行时权威在 Gate 快照**。
        * **3 个不在 schema 的键**（`review_floor` / `notify.*`）**归 YAML**（裁决 A2）：
          今天没有任何 AI 路径需要写它们 ⇒ 不搬进 Gate（不为不存在的需求换真相源）；
          但**改动记一条域事件**（`record_op("set_settings")`）⇒ **有痕、可查**。
        """
        from urllib.parse import quote

        s = container.settings
        gate_items = {
            "lookback_days": lookback_days,
            "scoring.threshold": threshold,
            "scoring.quota_per_topic": quota_per_topic,
            "scoring.max_papers": max_papers,
            "scoring.max_per_author": max_per_author,
            "scoring.must_read_cap": must_read_cap,
        }
        if stack is not None:
            denied = _gated("set_config_batch", items=gate_items,
                            reason="Web 设置页保存全局参数（6 个 Gate 标量）")
            if denied:
                # ⚠ 整批被拒 ⇒ **一个键都没落地**（set_batch 原子）⇒ YAML 也不写（别留半拉子）
                return RedirectResponse(f"/settings?msg={quote(denied)}", status_code=303)
        # 内存 settings 与 YAML 都跟着更新：Gate 是**运行时权威**，YAML 是**启动种子**
        s.lookback_days = lookback_days
        s.scoring.threshold = threshold
        s.scoring.quota_per_topic = quota_per_topic
        s.scoring.max_papers = max_papers
        s.scoring.max_per_author = max_per_author
        s.scoring.must_read_cap = must_read_cap
        s.scoring.review_floor = review_floor
        s.notify.enabled = notify_enabled
        s.notify.webhook_url = webhook_url
        save_settings(s)
        # 3 个"YAML 管辖"的键：留一条域痕（它们不在门的管辖内，面板也看不见它们——但要可查）
        container.repo.record_op(
            "set_settings", target="yaml",
            after={"review_floor": review_floor, "notify.enabled": notify_enabled,
                   "notify.webhook_url": webhook_url},
            actor="human", reason="Web 设置页保存全局参数（YAML 管辖的 3 键）")
        return RedirectResponse("/settings?msg=全局参数已保存（Gate 标量走门+审计；"
                                "review_floor/notify.* 归 YAML，改动已留痕）", status_code=303)

    @app.post("/settings/run")
    def trigger_run():
        started = run_in_background(
            container, stack=stack, actor="human", reason="Web 设置页手动触发"
        )
        msg = "已开始跑批，请稍后刷新查看简报" if started else "已有跑批任务在进行中"
        return RedirectResponse(f"/settings?msg={msg}", status_code=303)

    @app.post("/settings/profile/reset")
    def reset_profile_row(kind: str = Form("")):
        """重置兴趣画像（**人类专属**）：走命令面（写权门 + `command.reset_profile` 审计）。

        ⚠ AI 侧**没有**这个工具（`reset_profile` 刻意**不投影**给 AI——"改自己的标尺"那类动作），
        这里补的是**给人的入口**：从前"人类专属"却无人能用（人没有按钮，AI 也没工具 = 谁都用不了）。
        """
        from urllib.parse import quote

        msg = _gated("reset_profile", kind=kind,
                     reason=f"Web 设置页重置兴趣画像（{kind or '全部'}）")
        done = f"兴趣画像已重置（{kind or '全部'}）"
        return RedirectResponse(f"/settings?msg={quote(msg or done)}", status_code=303)

    @app.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request, actor: str = "", op: str = "", since_seq: int = 0):
        """记录仪：**两区**（账本角色退休后）。

        * 上区「**数据变更**」= 域账（`repo.events`：before→after + 撤销）——**只加不减**：
          撤销靠它的 `before`/`reversible`，**这张表不能退休**；
        * 下区「**操作审计**」= 框架账（`command.<name>` + actor + reason）——"谁做了什么"的家。
        两区按**实体标识**互引（域事件没有 call_id ⇒ 尽力而为，对不上是正常的）。
        """
        events = container.repo.events_since(
            since_seq=since_seq, actor=actor, op=op, limit=100
        )
        audit: list[dict] = []
        if stack is not None:
            # ⚠ **直读 mecha History**（不再经 cockpit 的 `/history` wire）：新形状里命令审计的
            # `target` 为空、命令名在 **`op`**，而 wire **不发 `op`** ⇒ 经 wire 会把命令名丢掉。
            # 这里只要"经门的操作"，所以按 `op` 前缀筛（顺带把 seed/state 事件排除在审计区外）。
            audit = [e.to_record() for e in stack["history"].events()
                     if str(getattr(e, "op", "")).startswith("command.")][-60:][::-1]
        return render(
            request,
            "activity.html",
            events=events["events"],
            last_seq=events["last_seq"],
            actor=actor,
            op=op,
            since_seq=since_seq,
            # ⚠ 无栈（单测/独立部署）时**不渲染撤销按钮**：不给做不到的承诺
            # （这页原先的病：文案说"可在 /activity 页 undo"，而页面上根本没有那个控件）。
            can_undo=stack is not None,
            audit=audit,
            linked=link_audit_to_targets(audit),
        )

    @app.post("/activity/undo")
    def undo_from_activity(seq: int = Form(0)):
        """记录仪一键撤销（人类面）：经**与论文写同一道门**（human 通道命令面 + 写权 + 审计）。

        `seq=0` = 最近一条可逆事件（命令面 `undo_change` 的语义）；不可逆的（入库/定稿）
        由命令面明确拒绝，消息原样回给页面。
        """
        from urllib.parse import quote

        if stack is None:
            return RedirectResponse(
                "/activity?msg=未接监控面：本 Web 未经统一启动入口装配 mecha 栈，无法撤销",
                status_code=303)
        msg = _gated("undo_change", seq=int(seq), reason="Web 记录仪一键撤销")
        return RedirectResponse(f"/activity?msg={quote(msg or f'已撤销 seq={int(seq)}')}",
                                status_code=303)

    # ------------------------------------------------- 写权控制端点（人类侧，口令 + side=human）
    # ⚠ `GET /monitor` 那个**服务端渲染的审计视图已退役**（2026-09-26）：
    # AI 监控改走 **dsh 共享资产的原生 tab**（`dsh-panel/`），Web 侧不再需要第二套视图
    # ⇒ 删页 + 删模板，**但控制端点 `POST /monitor/mode` 原样保留**（它不是面板，是
    # 人类控制端点；路径不动，UI 落点搬到 `/settings`）。
    @app.post("/monitor/mode")
    def switch_write_mode(target: str = Form("locked"), token: str = Form("")):
        """人类侧切写权模式：**口令 + 服务端钉 actor=human**（两道都要）。

        ⚠ **边界（如实写，不假装在防）**：这个口令挡的是**本机其他进程**，
        **挡不住有权读你文件的 AI**（它能读到 `~/.paperpilot/control-token`）。
        它存在的意义是让"控制端点不是谁都能按"成立，**不是**"AI 不能自授权"——
        后者靠口令放在**仓外**（AI 的文件访问通常被限在工作区）来尽量成立。

        **fail-closed**：口令文件不存在 / 口令空 / 不匹配 ⇒ **403 + 可读错误**，
        **绝不静默放行**（控制端点没有"默认放开"这一档）。

        成功/未接栈都重定向到 **`/settings`**（本卡的新落点；`/monitor` 已不存在）。
        """
        if stack is None:
            return RedirectResponse("/settings?msg=未接监控面", status_code=303)
        from urllib.parse import quote

        from .control_token import verify_control_token

        if not verify_control_token(token):
            detail = ("本实例未发布控制口令（`paperpilot serve` / `ai` 启动时会打印一个，"
                      "存在用户家目录 `~/.paperpilot/control-token`）"
                      if not token else "口令不对")
            return HTMLResponse(
                status_code=403,
                content=(
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>403 写权切换被拒</title>"
                    "<div style='font:14px/1.7 system-ui;max-width:44em;margin:3em auto'>"
                    "<h1 style='font-size:18px'>403：写权切换被拒</h1>"
                    f"<p>{detail}。</p>"
                    "<p>控制端点采取 <b>fail-closed</b>：没有口令就一律拒绝，不会默认放开。"
                    "口令在启动 `paperpilot serve` / `paperpilot ai` 的那个控制台里，"
                    "也可以直接看 <code>~/.paperpilot/control-token</code>"
                    "（<b>刻意放在仓外</b>：本机制挡的是本机其他进程，"
                    "挡不住能读你文件的 AI——这条边界是明说的，不是默认的）。</p>"
                    "<p><a href='/settings'>← 回到设置页（写权模式卡在那儿）</a></p>"
                    "</div>"),
            )

        from mecha.authority import Mode
        try:
            stack["authority"].switch_mode(Mode(target), side="human")
            msg = f"写权已切到 {target}"
        except Exception as exc:  # noqa: BLE001 - 展示可教学拒绝（含非法 target）
            msg = f"切换失败：{exc}"
        return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)

    # ---------------------------------------------------------------- 健康检查
    @app.get("/healthz")
    def healthz():
        return {
            "ok": True,
            "ai_provider": container.ai_provider,
            "running": container.run_state["running"],
        }

    # --------------------------------------------------- cockpit JSON（供 dsh 面板跨源轮询）
    # dsh 面板（origin :3081）先经 host 同源路由 `/paperpilot/monitor-url` 从 `.web-port`
    # 解析到本 Web 的 base，再 GET `/history` + `/config`（mecha cockpit 契约）。跨源 ⇒
    # 必带 `Access-Control-Allow-Origin`；无栈（独立 Web）⇒ 503 可读错误，绝不空白/不回落。
    def _cockpit_json(payload: dict, status: int = 200):
        from fastapi.responses import JSONResponse

        return JSONResponse(payload, status_code=status,
                            headers={"Access-Control-Allow-Origin": "*"})

    def _cockpit_source():
        from mecha.cockpit import MonitorSource

        from ..mecha_adapter.monitor import config_schema_rows

        return MonitorSource.from_software(
            stack["software"], schema_rows=config_schema_rows)

    @app.get("/history")
    def monitor_history(since_seq: int = 0):
        if stack is None:
            return _cockpit_json(
                {"ok": False, "error": "未接监控面（未经统一启动入口装配 mecha 栈）"}, 503)
        from mecha.cockpit import history_records

        mode = str(getattr(stack["authority"].mode, "value", stack["authority"].mode))
        return _cockpit_json({"ok": True,
                              "events": history_records(_cockpit_source(), since_seq),
                              "mode": mode})

    @app.get("/config")
    def monitor_config():
        if stack is None:
            return _cockpit_json(
                {"ok": False, "error": "未接监控面（未经统一启动入口装配 mecha 栈）"}, 503)
        from mecha.cockpit import config_tree

        return _cockpit_json({"ok": True, **config_tree(_cockpit_source())})

    return app


# ---------------------------------------------------------------- 辅助
def _pretty_json(value) -> str:
    """把事件 before/after 字典美化成可读 JSON（解码 unicode + 缩进）；空值返回空串。"""
    import json

    if not value:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def _back(arxiv_id: str, msg: str = "") -> str:
    from urllib.parse import quote

    return _with_msg(f"/papers/{quote(arxiv_id)}", msg)


def _with_msg(url: str, msg: str) -> str:
    """把提示消息拼到重定向 URL 的 ?msg=（空消息不拼）。"""
    if not msg:
        return url
    from urllib.parse import quote

    sep = "&" if "?" in url else "?"
    return f"{url}{sep}msg={quote(msg)}"


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _topic_from_form(
    name: str,
    description: str,
    keywords: str,
    exclude_keywords: str,
    categories: str,
    authors: str,
    weight: float = 0.5,
) -> TopicCfg:
    """设置页的「手动新建主题包」→ TopicCfg。

    ⚠ 2026-09-30：`quota`/`threshold`/`enabled` 不再由表单提供——主题的语义只剩
    "按 weight 往画像池注入词条"（见 TopicCfg 文档）。旧字段仍留在模型里读得懂老 YAML。
    """
    return TopicCfg(
        name=name.strip() or "未命名主题",
        description=description,
        keywords=_split_csv(keywords),
        exclude_keywords=_split_csv(exclude_keywords),
        categories=_split_csv(categories),
        authors=_split_csv(authors),
        weight=max(0.0, float(weight or 0.0)),
        enabled=True,
    )

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

#: 域事件 ↔ 框架账 的**互引键**：框架事件的 `value` 里带这些实体标识，域事件的 `target` 是同一个标识。
#: ⚠ 域事件**没有** `call_id`（`repo._event` 的字段里就没有它）⇒ 互引只能**按实体标识**尽力而为：
#: 匹配不上是**正常**的（读操作 / 被门拒 / 启动痕 / YAML 键都没有命令审计）。
_AUDIT_REF_KEYS = ("arxiv_id", "note_id", "topic", "added", "date", "run_id",
                   "briefing_id", "path", "undone_seq", "seq")


def link_audit_to_targets(audit: list[dict]) -> dict[str, list[str]]:
    """框架账 → ``{域 target: [命令键, …]}``：把两册**对上号**（对不上的就不出现在结果里）。

    为什么单独一个纯函数：页面渲染难断言，而"**能对上号**"是这次给用户的核心价值
    ⇒ 把它做成可直测的映射（判据 `test_activity_links_domain_events_to_framework_audit`）。
    """
    linked: dict[str, list[str]] = {}
    for rec in audit:
        value = rec.get("value")
        if not isinstance(value, dict):
            continue
        for ref in _AUDIT_REF_KEYS:
            got = value.get(ref)
            if isinstance(got, (str, int)) and str(got):
                linked.setdefault(str(got), []).append(str(rec.get("target") or ""))
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
        return render(request, "paper_detail.html", detail=detail, arxiv_id=arxiv_id)

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

    # ---------------------------------------------------------------- 图底座实例（F1-F5：展示侧；改图的手在 AI）
    @app.get("/network", response_class=HTMLResponse)
    def network(request: Request, focus: str = ""):
        """引文网络 = 图底座的第一个实例。层号是拓扑属性（从在库论文出发的最短路径深度），
        无限层；节点是站内句柄（F3：点击=入库+进管理页，永不外跳）；hover 卡从卡片系统
        拉内容（F4，图不生产文字）；呈现参数全在 settings.graph（F5）。域层无领域词。"""
        from ..domain.graph import Edge as GEdge
        from ..domain.graph import Node as GNode
        from ..domain.graph import label_for, layered_layout
        from ..domain.profile import paper_features, score_paper
        g = container.settings.graph
        edges = container.repo.citation_edges_all(limit=1500)
        if not edges:
            return render(request, "network.html", nodes=[], links=[], focus=focus,
                          height=200, width=980,
                          stats={"edges": 0, "src": 0, "dst": 0, "shown": 0, "layers": 0},
                          msg="引文图谱还空着：这页是 AI 调研成果的显示器——"
                              "在 dsh 让 AI 对关键论文 sync_citations，调查完回这里看图")
        dst_info: dict[str, dict] = {}
        src_out: dict[str, int] = {}
        for e in edges:
            d = dst_info.setdefault(e.dst_arxiv_id, {"title": e.dst_title, "cites": 0,
                                                     "citations": e.dst_citations})
            d["cites"] += 1
            src_out[e.src_arxiv_id] = src_out.get(e.src_arxiv_id, 0) + 1
        weights = container.repo.profile_weights_map()
        # 消费方适配器：CitationEdge → Node/Edge（F1；域层不认识“引用”）
        nodes_g: list[GNode] = []
        for sid in src_out:
            p = container.repo.get_paper(sid)
            if p is None:
                continue
            feat = paper_features(list(p.categories or []), p.primary_category,
                                  p.title or "", p.abstract or "", list(p.authors or []))
            sc, why = score_paper(feat, weights)
            nodes_g.append(GNode(id=sid, kind="src", weight=sc, meta={
                "kind": "src", "title": p.title or sid,
                "published": p.published_at.date().isoformat() if p.published_at else "",
                "in_lib": True, "sc": sc, "why0": why[0] if why else ""}))
        for did, info in dst_info.items():
            if did in src_out:
                continue                                   # 在库身份优先（同人去重）
            nodes_g.append(GNode(id=did, kind="dst",
                                 weight=float(info["citations"]) / 1000.0, meta={
                "kind": "dst", "title": info["title"] or did, "published": "",
                "in_lib": False, "cites": info["cites"], "citations": info["citations"]}))
        gedges = [GEdge(src=e.src_arxiv_id, dst=e.dst_arxiv_id,
                        weight=float(e.dst_citations or 0),
                        kind="infl" if e.influential else "") for e in edges]
        lay = layered_layout(nodes_g, gedges, sources=set(src_out),
                             layer_gap=int(g.layer_gap), node_gap=int(g.node_gap),
                             max_nodes=int(g.max_nodes), sort_within=g.sort_within,
                             size_by=g.size_by)
        meta_by = {n.id: n.meta for n in nodes_g}
        nodes: list[dict] = []
        for nid, pt in lay["pos"].items():
            m = meta_by.get(nid) or {}
            if g.color_by == "in_lib":
                fill = "var(--accent)" if m.get("in_lib") else "#64748b"
            elif g.color_by == "weight":
                fill = "var(--accent)" if m.get("in_lib") else "#94a3b8"
            else:
                fill = "var(--accent)" if m.get("kind") == "src" else "#64748b"
            nodes.append({**pt, "id": nid,
                          "label": label_for(m.get("title", ""), nid, max_len=int(g.max_label_len)),
                          "fill": fill, "card": _node_hover_card(nid, m)})
        pos = lay["pos"]
        # 边采样：全画必成蜘蛛网（实测 684 条糊屏）——按权重（S2 被引数）取 top-K。
        keep_edges = [le for le in lay["edges"] if le["keep"]]
        if len(keep_edges) > int(g.max_edges):
            keep_edges = sorted(keep_edges, key=lambda le: -le["weight"])[:int(g.max_edges)]
        links = [{"x1": pos[lnk["src"]]["x"], "y1": pos[lnk["src"]]["y"],
                  "x2": pos[lnk["dst"]]["x"], "y2": pos[lnk["dst"]]["y"],
                  "op": (0.75 if (not focus) or focus in (lnk["src"], lnk["dst"]) else 0.12),
                  "infl": lnk["kind"] == "infl"}
                 for lnk in keep_edges]
        return render(request, "network.html", nodes=nodes, links=links,
                      focus=focus, msg=request.query_params.get("msg", ""),   # 跳转带话要接得住
                      width=lay["width"], height=lay["height"],
                      stats={"edges": len(edges), "src": len(src_out),
                             "dst": len(dst_info), "shown": len(nodes),
                             "layers": lay["layers"], "edges_shown": len(links)})

    def _node_hover_card(nid: str, m: dict) -> dict:
        """F4 富化协议：图不生产内容——文字从卡片系统（summary/score）与消费方 meta 拉。"""
        facts: list[str] = []
        if m.get("in_lib"):
            facts.append(f"画像分 {m.get('sc', 0.0):.2f}")
            detail = container.retrieval.detail(nid)
            s = detail["summary"] if detail else None
            lines = []
            if s:
                for tag, val in (("TL;DR", s.tldr), ("问题", s.problem), ("方法", s.method),
                                 ("结论", s.results), ("贡献", s.novelty)):
                    if val:
                        lines.append(f"{tag}：{val}")
            else:
                lines = ["还没卡：在 dsh 让 AI write_summary 补一张"]
        else:
            facts += [f"库内同引 {m.get('cites', 0)} 篇", f"S2 被引 {m.get('citations', 0)}"]
            lines = ["未入库：点击＝拉进入库并进管理页，卡由 AI 按需补"]
        return {"title": m.get("title") or nid, "facts": facts, "lines": lines}

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
                   node_gap: int = Form(90), max_nodes: int = Form(40),
                   max_edges: int = Form(220),
                   size_by: str = Form("degree"), color_by: str = Form("kind"),
                   sort_within: str = Form("weight")):
        """图呈现参数（F5）：不进 gate schema——纯展示项，YAML 即唯一真相。"""
        from ..config import save_settings
        s = container.settings
        try:
            s.graph.max_label_len = max(6, int(max_label_len))
            s.graph.layer_gap = max(40, int(layer_gap))
            s.graph.node_gap = max(24, int(node_gap))
            s.graph.max_nodes = max(8, int(max_nodes))
            s.graph.max_edges = max(20, int(max_edges))
        except (TypeError, ValueError):
            return RedirectResponse(_with_msg("/settings", "图参数需为整数"), status_code=303)
        s.graph.size_by = size_by if size_by in ("degree", "weight", "flat") else "degree"
        s.graph.color_by = color_by if color_by in ("kind", "in_lib", "weight") else "kind"
        s.graph.sort_within = sort_within if sort_within in ("weight", "year") else "weight"
        save_settings(s)
        return RedirectResponse(_with_msg("/settings", "图参数已存，下次渲染生效（层数不在内：那是拓扑）"),
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
        return render(
            request,
            "settings.html",
            settings=s,
            last_run=container.repo.last_run(),
            counts=container.repo.counts_by_status(),
            briefings=container.repo.briefings(limit=30),
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
        quota: int = Form(4),
        threshold: float = Form(0.6),
        enabled: bool = Form(False),
    ):
        s = container.settings
        topics = list(s.topics)
        if action == "add":
            topics.append(_topic_from_form(name, description, keywords, exclude_keywords,
                                           categories, authors, quota, threshold, enabled))
            msg = f"已新增主题「{name}」"
        elif action == "update" and index.isdigit() and 0 <= int(index) < len(topics):
            topics[int(index)] = _topic_from_form(
                name, description, keywords, exclude_keywords,
                categories, authors, quota, threshold, enabled,
            )
            msg = f"已更新主题「{name}」"
        elif action == "delete" and index.isdigit() and 0 <= int(index) < len(topics):
            removed = topics.pop(int(index))
            msg = f"已删除主题「{removed.name}」"
        else:
            return RedirectResponse("/settings?msg=无效操作", status_code=303)
        s.topics = topics
        save_settings(s)
        container.repo.sync_topics(
            topics, actor="human",
            reason=f"Web 设置页主题操作（{action}）「{name}」",
        )
        return RedirectResponse(f"/settings?msg={msg}", status_code=303)

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
            from mecha.cockpit import history_records

            audit = list(history_records(_cockpit_source(), 0))[-60:][::-1]
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
    quota: int,
    threshold: float,
    enabled: bool,
) -> TopicCfg:
    return TopicCfg(
        name=name.strip() or "未命名主题",
        description=description,
        keywords=_split_csv(keywords),
        exclude_keywords=_split_csv(exclude_keywords),
        categories=_split_csv(categories),
        authors=_split_csv(authors),
        quota=quota,
        threshold=threshold,
        enabled=enabled,
    )

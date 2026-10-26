"""把 AI 写的草稿渲染成页面。

五个页面：
- `/`         线性页（默认草稿）
- `/network`  网络页（默认草稿）
- `/profile`  画像池（只读 + 锚点增删）
- `/dashboard` 仪表盘（只读）
- `/settings` 设置（草稿管理 + 系统参数）

叶子页：`/papers/{arxiv_id}`。
**所有写操作都经命令面**（`stack.invoke_human`）——面板不直写数据库，
否则就成了绕过门与审计的第二条业务路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import canvas, drafts as draft_lib, graphviz, scoring
from ..mecha_adapter import config as config_mod
from ..mecha_adapter.stack import Stack

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
LANE_LABEL = {"category": "分类", "term": "词面", "author": "作者"}


def _fmt(ts: str) -> str:
    return (ts or "")[:16]


def create_app(stack: Stack) -> FastAPI:
    app = FastAPI(title="PaperPilot")
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    TEMPLATES.env.filters["fmt"] = _fmt

    repo, settings = stack.repo, stack.settings

    def render(request: Request, name: str, **context: Any) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, name, {"request": request, "nav": _nav(request), **context})

    def page_draft(kind: str, wanted: int | None = None) -> dict | None:
        """这一页显示哪一份：显式 `?draft=` > 设置里的默认 > 最近一份同类型的。

        草稿按 `kind` 分家（线性 / 网络各看各的），所以显式指定也必须同 kind，
        否则落回默认——跨页指定是输入错误，不是"打开一个不存在的草稿"。
        """
        if wanted:
            chosen = repo.draft(int(wanted))
            if chosen is not None and chosen["kind"] == kind:
                return chosen
        ident = repo.setting(f"page.{kind}.default", "")
        draft = repo.draft(ident) if ident else None
        if draft is None or draft["kind"] != kind:
            candidates = repo.drafts(kind=kind)
            draft = candidates[0] if candidates else None
        return draft

    def sidebar(kind: str, current: dict | None) -> dict:
        """侧边栏：本页所有存档草稿的标题，点一条就打开那一条。"""
        default_id = repo.setting(f"page.{kind}.default", "")
        items = []
        for draft in repo.drafts(kind=kind):
            described = draft_lib.describe(draft)
            items.append({"id": described["id"], "title": described["title"],
                          "count": described["count"],
                          "updated_at": described["updated_at"],
                          "is_default": str(described["id"]) == default_id})
        return {"drafts": items, "current": current["id"] if current else None,
                "base": "/" if kind == "linear" else "/network",
                "kind_label": "线性" if kind == "linear" else "网络"}

    def card_of(kind: str, draft: dict | None) -> list[dict]:
        if draft is None or kind != "linear":
            return []
        entries = (draft["payload"] or {}).get("entries") or []
        ids = [entry["arxiv_id"] for entry in entries]
        papers = repo.papers_by_ids(ids)
        cards = repo.cards_of(ids)
        stars = repo.star_map(ids)
        out = []
        for entry in entries:
            aid = entry["arxiv_id"]
            paper = papers.get(aid) or {"arxiv_id": aid, "title": aid}
            out.append({"paper": paper, "card": cards.get(aid),
                        "starred": bool(stars.get(aid)), "why": entry.get("why", ""),
                        "group": entry.get("group", "")})
        return out

    # ---------------------------------------------------------------- 线性页
    @app.get("/", response_class=HTMLResponse)
    def linear(request: Request, draft: int | None = None):
        current = page_draft("linear", draft)
        return render(request, "linear.html", draft=current,
                      cards=card_of("linear", current),
                      sidebar=sidebar("linear", current),
                      msg=request.query_params.get("msg", ""))

    # ---------------------------------------------------------------- 网络页
    @app.get("/network", response_class=HTMLResponse)
    def network(request: Request, draft: int | None = None):
        current = page_draft("network", draft)
        graph = _layout(current) if current else {"nodes": [], "edges": [], "width": 0,
                                                  "height": 0}
        return render(request, "network.html", draft=current, graph=graph,
                      sidebar=sidebar("network", current),
                      msg=request.query_params.get("msg", ""))

    # ---------------------------------------------------------------- 画像池
    @app.get("/profile", response_class=HTMLResponse)
    def profile(request: Request, top: int = 12):
        view = scoring.profile_view(repo, settings, top=int(top))
        return render(request, "profile.html", view=view, lane_label=LANE_LABEL,
                      settings=settings, mode=stack.mode,
                      msg=request.query_params.get("msg", ""))

    # ---------------------------------------------------------------- 仪表盘
    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, op: str = "", actor: str = "", limit: int = 80):
        events = repo.events(limit=int(limit), op_prefix=op, actor=actor)
        counts = {
            "papers": repo.count_papers(),
            "cards": repo.conn.execute("SELECT COUNT(*) c FROM cards").fetchone()["c"],
            "starred": len(repo.starred_ids()),
            "drafts": len(repo.drafts()),
            "events": repo.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"],
            "scanned": len(repo.scanned_days()),
            "edges": repo.edge_counts()["edges"],
        }
        signals = repo.conn.execute(
            "SELECT op, COUNT(*) c FROM events WHERE op LIKE 'signal:%' GROUP BY op"
            " ORDER BY c DESC").fetchall()
        return render(request, "dashboard.html", events=events, counts=counts,
                      signals=[{"op": r["op"], "count": r["c"]} for r in signals],
                      op=op, actor=actor, mode=stack.mode)

    # ---------------------------------------------------------------- 设置
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        drafts = [draft_lib.describe(d) for d in repo.drafts()]
        view = stack.software.monitor.read()
        return render(request, "settings.html", drafts=drafts, settings=settings,
                      config_keys=list(config_mod.CONFIG_KEYS),
                      defaults={"linear": repo.setting("page.linear.default", ""),
                                "network": repo.setting("page.network.default", "")},
                      mode=stack.mode, monitor=view,
                      msg=request.query_params.get("msg", ""))

    # ---------------------------------------------------------------- 叶子页
    @app.get("/papers/{arxiv_id}", response_class=HTMLResponse)
    def paper_detail(request: Request, arxiv_id: str):
        view = repo.paper_view(arxiv_id)
        if view is None:
            return render(request, "missing.html", arxiv_id=arxiv_id)
        repo.record(op="signal:view", target=view["arxiv_id"], actor="human",
                    after={"weight": settings.signal_weights["view"]})
        return render(request, "paper.html", paper=view,
                      notes=repo.notes(view["arxiv_id"]),
                      msg=request.query_params.get("msg", ""))

    # ---------------------------------------------------------------- 写（经命令面）
    def run(request: Request, name: str, args: dict, *, back: str = "/") -> str:
        """调一条命令；失败把可教学错误带回页面。"""
        result = stack.invoke_human(name, **args)
        if result.get("is_error"):
            info = (result.get("error") or {}).get("info") or {}
            hint = info.get("hint") or ""
            return f"{name} 被拒绝：{info.get('kind', '')} {hint}".strip()
        return ""

    def redirect(target: str, msg: str = "") -> RedirectResponse:
        from urllib.parse import quote
        url = f"{target}?msg={quote(msg)}" if msg else target
        return RedirectResponse(url, status_code=303)

    # ---------------------------------------------------------------- 出站跳转
    @app.get("/papers/{arxiv_id}/go")
    def go_arxiv(arxiv_id: str):
        """去 arXiv abs 页：**先记 outbound 信号再 302**。

        为什么要绕一站：直接给 `arxiv.org` 超链接，点击就离开了系统，
        而 `outbound`（0.5）和 `download`（1.0）是权重最重的两个正向信号——
        绕过系统就等于永远采不到。
        """
        paper = repo.paper(arxiv_id) or {}
        target = paper.get("abs_url") or f"https://arxiv.org/abs/{arxiv_id}"
        repo.record(op="signal:outbound", target=arxiv_id, actor="human",
                    after={"weight": settings.signal_weights["outbound"]})
        return RedirectResponse(target, status_code=303)

    @app.get("/papers/{arxiv_id}/pdf")
    def download_pdf(arxiv_id: str):
        """直下 PDF：**先记 download 信号再 302**（文件不落地，不本地缓存）。"""
        paper = repo.paper(arxiv_id) or {}
        target = paper.get("pdf_url") or f"https://arxiv.org/pdf/{arxiv_id}"
        repo.record(op="signal:download", target=arxiv_id, actor="human",
                    after={"weight": settings.signal_weights["download"]})
        return RedirectResponse(target, status_code=303)

    @app.post("/papers/{arxiv_id}/star")
    def toggle_star(arxiv_id: str, next_url: str = Form("")):
        current = repo.paper_view(arxiv_id) or {}
        starred = bool((current.get("reading") or {}).get("starred"))
        msg = run(request=None, name="star_paper",
                  args={"arxiv_id": arxiv_id, "starred": not starred})
        return redirect(next_url or f"/papers/{arxiv_id}", msg)

    @app.post("/papers/{arxiv_id}/skip")
    def skip(arxiv_id: str, next_url: str = Form("")):
        msg = run(request=None, name="skip_paper", args={"arxiv_id": arxiv_id})
        return redirect(next_url or f"/papers/{arxiv_id}", msg)

    @app.post("/drafts/{draft_id}/delete")
    def delete_draft(draft_id: int):
        msg = run(request=None, name="delete_draft", args={"draft_id": draft_id})
        return redirect("/settings", msg)

    @app.post("/drafts/{draft_id}/rename")
    def rename_draft(draft_id: int, title: str = Form(...)):
        msg = run(request=None, name="rename_draft",
                  args={"draft_id": draft_id, "title": title.strip()})
        return redirect("/settings", msg)

    @app.post("/settings/default")
    def set_default(kind: str = Form(...), draft_id: str = Form("")):
        value = int(draft_id) if draft_id.strip().isdigit() else 0
        msg = run(request=None, name="set_default_draft",
                  args={"kind": kind, "draft_id": value})
        return redirect("/settings", msg)

    @app.post("/settings/config")
    def set_config_form(half_life_days: float = Form(30.0),
                        default_regions_n: int = Form(5),
                        max_pages_per_day: int = Form(4),
                        max_requests: int = Form(120),
                        recent_draft_days: float = Form(7.0)):
        messages = []
        for key, value in (("half_life_days", half_life_days),
                           ("default_regions_n", default_regions_n),
                           ("max_pages_per_day", max_pages_per_day),
                           ("max_requests", max_requests),
                           ("recent_draft_days", recent_draft_days)):
            msg = run(request=None, name="set_config", args={"key": key, "value": value})
            if msg:
                messages.append(msg)
        return redirect("/settings", "；".join(messages))

    @app.post("/profile/anchor")
    def add_anchor_form(dimension: str = Form(...), key: str = Form(...),
                        weight: float = Form(1.0)):
        msg = run(request=None, name="add_anchor",
                  args={"dimension": dimension, "key": key.strip(), "weight": float(weight)})
        return redirect("/profile", msg)

    @app.post("/profile/anchor/delete")
    def remove_anchor_form(dimension: str = Form(...), key: str = Form(...)):
        msg = run(request=None, name="remove_anchor",
                  args={"dimension": dimension, "key": key})
        return redirect("/profile", msg)

    return app


def _nav(request: Request) -> list[dict]:
    pages = [("/", "线性"), ("/network", "网络"), ("/profile", "画像池"),
             ("/dashboard", "仪表盘"), ("/settings", "设置")]
    path = request.url.path
    return [{"href": href, "label": label, "active": path == href} for href, label in pages]


def _layout(draft: dict) -> dict:
    """草稿 → 画布几何：**谁画在哪交给 dot**，页面只管样式与跳转。

    以前这里手排网格（层=列、组内按 id 排），于是"想到的关系"被压成一排排点，
    文字节点更是无处可放。现在元素表丢给 Graphviz 算坐标（``-Tplain`` 回几何），
    元素类型不受限：论文、文字框、便签、分组框都行；论文节点才带进详情页的链接。
    """
    payload = draft.get("payload") or {}
    nodes, edges = canvas.normalize(payload)
    spec = payload.get("layout") or {}
    geo = graphviz.layout(canvas.to_dot(
        nodes, edges, rankdir="LR" if spec.get("rankdir") == "LR" else "TB"))
    if not geo["ok"]:
        return {"nodes": [], "edges": [], "frames": [], "width": 0, "height": 0,
                "error": geo["error"]}
    by_id = {n.id: n for n in nodes}
    out_nodes = []
    for nid, box in geo["nodes"].items():
        node = by_id.get(nid)
        if node is None:                      # dot 生出来的点不是元素（不该发生）
            continue
        out_nodes.append({
            "id": nid, "label": node.label, "kind": node.kind,
            "arxiv_id": node.arxiv, "link": node.link, "shape": node.shape_name,
            "lines": canvas.wrap_label(node.label),
            "x": box["x"], "y": box["y"], "w": box["w"], "h": box["h"],
            "fill": canvas.fill_of(node), "color": node.color,
        })
    out_edges = [{"src": e["src"], "dst": e["dst"], "label": e["label"], "d": e["d"],
                  "lx": e["lx"], "ly": e["ly"]}
                 for e in geo["edges"] if e.get("d")]
    # 框：`-Tplain` **不给** cluster 的几何，所以自己算成员的外包框（比解析 SVG 便宜）。
    # 框画在元素之前（垫底），标题压在左上角。
    frames = []
    for group in (n for n in nodes if n.is_group):
        members = [box for nid, box in geo["nodes"].items()
                   if by_id.get(nid) is not None and by_id[nid].parent == group.id]
        if not members:
            continue
        x0 = min(b["x"] - b["w"] / 2 for b in members) - 14
        x1 = max(b["x"] + b["w"] / 2 for b in members) + 14
        y0 = min(b["y"] - b["h"] / 2 for b in members) - 26
        y1 = max(b["y"] + b["h"] / 2 for b in members) + 12
        frames.append({"id": group.id, "label": group.label,
                       "x0": round(x0), "y0": round(y0), "x1": round(x1), "y1": round(y1),
                       "fill": canvas.fill_of(group), "color": group.color})
    return {"nodes": out_nodes, "edges": out_edges, "frames": frames,
            "width": round(geo["width"]), "height": round(geo["height"]), "error": ""}

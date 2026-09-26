"""FastAPI Web 应用：本地面板（今日简报 / 论文库 / 详情 / 设置）。

不依赖任何 CDN（离线可用）；交互全部用原生表单 POST + 303 重定向。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..capabilities import registry_for
from ..config import TopicCfg, save_settings
from .container import Container, run_in_background

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def create_app(container: Container) -> FastAPI:
    app = FastAPI(title="PaperPilot", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["prettyjson"] = _pretty_json
    app.state.container = container
    app.state.templates = templates

    def render(request: Request, template: str, **ctx) -> HTMLResponse:
        ctx.setdefault("request", request)
        ctx.setdefault("container", container)
        ctx.setdefault("msg", request.query_params.get("msg", ""))
        return templates.TemplateResponse(request, template, ctx)

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
        return render(request, "paper_detail.html", detail=detail, arxiv_id=arxiv_id)

    # ---------------------------------------------------------------- 论文动作
    @app.post("/papers/{arxiv_id}/read")
    def toggle_read(arxiv_id: str):
        container.retrieval.toggle_read(arxiv_id)
        return RedirectResponse(_back(arxiv_id), status_code=303)

    @app.post("/papers/{arxiv_id}/star")
    def toggle_star(arxiv_id: str):
        container.retrieval.star(arxiv_id)
        return RedirectResponse(_back(arxiv_id), status_code=303)

    @app.post("/papers/{arxiv_id}/skip")
    def mark_skip(arxiv_id: str):
        container.retrieval.skip(arxiv_id)
        return RedirectResponse(_back(arxiv_id), status_code=303)

    @app.post("/papers/{arxiv_id}/note")
    def add_note(arxiv_id: str, content: str = Form(...)):
        container.retrieval.add_note(arxiv_id, content)
        return RedirectResponse(_back(arxiv_id), status_code=303)

    @app.post("/notes/{note_id}/delete")
    def delete_note(note_id: int, arxiv_id: str = Form(...)):
        container.retrieval.delete_note(note_id)
        return RedirectResponse(_back(arxiv_id), status_code=303)

    # ---------------------------------------- 下载归档（经中性能力层，界面②调 Python API）
    @app.post("/papers/{arxiv_id}/download")
    def download_paper(arxiv_id: str, next_url: str = Form("")):
        from urllib.parse import quote

        result = registry_for(container).invoke(
            "download_paper", arxiv_id=arxiv_id, actor="human", reason="Web 界面下载归档"
        )
        if result.get("ok"):
            msg = "已下载归档到本地" + ("（此前已下载）" if result.get("cached") else "")
        else:
            msg = f"下载失败：{result.get('error', {}).get('message', '未知错误')}"
        target = next_url or _back(arxiv_id)
        return RedirectResponse(f"{target}?msg={quote(msg)}", status_code=303)

    @app.get("/papers/{arxiv_id}/pdf")
    def local_pdf(arxiv_id: str):
        path = container.settings.pdf_dir / f"{arxiv_id}.pdf"
        if path.exists():
            return FileResponse(
                str(path), media_type="application/pdf", filename=f"{arxiv_id}.pdf"
            )
        detail = container.retrieval.detail(arxiv_id)
        url = (detail["paper"].pdf_url if detail else "") or f"https://arxiv.org/pdf/{arxiv_id}"
        return RedirectResponse(url)

    # ---------------------------------------------------------------- 设置
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        s = container.settings
        return render(
            request,
            "settings.html",
            settings=s,
            last_run=container.repo.last_run(),
            counts=container.repo.counts_by_status(),
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

    @app.post("/settings/general")
    def save_general(
        lookback_days: int = Form(7),
        threshold: float = Form(0.6),
        quota_per_topic: int = Form(4),
        max_papers: int = Form(12),
        max_per_author: int = Form(1),
        must_read_cap: int = Form(3),
        daily_at: str = Form("07:30"),
        schedule_enabled: bool = Form(False),
        notify_enabled: bool = Form(False),
        webhook_url: str = Form(""),
    ):
        s = container.settings
        s.lookback_days = lookback_days
        s.scoring.threshold = threshold
        s.scoring.quota_per_topic = quota_per_topic
        s.scoring.max_papers = max_papers
        s.scoring.max_per_author = max_per_author
        s.scoring.must_read_cap = must_read_cap
        s.schedule.daily_at = daily_at
        s.schedule.enabled = schedule_enabled
        s.notify.enabled = notify_enabled
        s.notify.webhook_url = webhook_url
        save_settings(s)
        return RedirectResponse("/settings?msg=全局参数已保存（AI provider/daily_at 重启后生效）", status_code=303)

    @app.post("/settings/run")
    def trigger_run():
        started = run_in_background(container, actor="human", reason="Web 设置页手动触发")
        msg = "已开始跑批，请稍后刷新查看简报" if started else "已有跑批任务在进行中"
        return RedirectResponse(f"/settings?msg={msg}", status_code=303)

    @app.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request, actor: str = "", op: str = "", since_seq: int = 0):
        """记录仪（L5 监控面 = 事件总线的只读投影；GAPS.md §3）。"""
        events = container.repo.events_since(
            since_seq=since_seq, actor=actor, op=op, limit=100
        )
        return render(
            request,
            "activity.html",
            events=events["events"],
            last_seq=events["last_seq"],
            actor=actor,
            op=op,
            since_seq=since_seq,
        )

    # ---------------------------------------------------------------- 健康检查
    @app.get("/healthz")
    def healthz():
        return {
            "ok": True,
            "ai_provider": container.ai_provider,
            "running": container.run_state["running"],
        }

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


def _back(arxiv_id: str) -> str:
    from urllib.parse import quote

    return f"/papers/{quote(arxiv_id)}"


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

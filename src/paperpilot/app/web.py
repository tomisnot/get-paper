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
        return render(request, "paper_detail.html", detail=detail, arxiv_id=arxiv_id)

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
        # 无对应命令（删笔记是人类独有的管理操作、不与 AI 争写）：保持直调。
        container.retrieval.delete_note(note_id)
        return RedirectResponse(_back(arxiv_id), status_code=303)

    # ---------------------------------------- 下载归档（经中性能力层/命令面）
    @app.post("/papers/{arxiv_id}/download")
    def download_paper(arxiv_id: str, next_url: str = Form("")):
        target = next_url or _back(arxiv_id)
        if stack is None:
            result = registry_for(container).invoke(
                "download_paper", arxiv_id=arxiv_id, actor="human", reason="Web 界面下载归档"
            )
            if result.get("ok"):
                msg = "已下载归档到本地" + ("（此前已下载）" if result.get("cached") else "")
            else:
                msg = f"下载失败：{result.get('error', {}).get('message', '未知错误')}"
            return RedirectResponse(_with_msg(target, msg), status_code=303)
        from ..mecha_adapter.hub import human_write

        res = human_write(stack, "download_paper", arxiv_id=arxiv_id, reason="Web 界面下载归档")
        if res.get("is_error"):
            info = res["error"].get("info", {})
            msg = f"下载失败：{res['error'].get('message', '')}（{info.get('kind', '')}）"
        else:
            value = res.get("value", {})
            msg = "已下载归档到本地" + ("（此前已下载）" if value.get("cached") else "")
        return RedirectResponse(_with_msg(target, msg), status_code=303)

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
        started = run_in_background(
            container, stack=stack, actor="human", reason="Web 设置页手动触发"
        )
        msg = "已开始跑批，请稍后刷新查看简报" if started else "已有跑批任务在进行中"
        return RedirectResponse(f"/settings?msg={msg}", status_code=303)

    @app.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request, actor: str = "", op: str = "", since_seq: int = 0):
        """记录仪（域数据面 = repo.events 的 before→after delta + undo；L5 监控面）。"""
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

    @app.get("/monitor", response_class=HTMLResponse)
    def monitor_page(request: Request):
        """操作者监控面（mecha cockpit 的人话视图，供 dsh 侧边栏 iframe 挂载）。

        与 `/activity`（域数据面 = repo.events）互补：本页是 **操作者审计面** =
        mecha History（command.<name> 审计 + 配置态 KV）+ 概括（可被原始证伪）。
        无 stack（独立测试）时如实报“未接监控面”，不假装空。
        """
        from .control_token import read_control_token

        control_ready = read_control_token() is not None
        if stack is None:
            return render(request, "monitor.html", view=None, mode=None,
                          events=[], config=None, stack_ready=False,
                          control_ready=control_ready)
        from mecha.cockpit import (
            MonitorSource,
            activity_records,
            config_tree,
            monitor_summary_payload,
        )

        from ..mecha_adapter.monitor import config_schema_rows

        source = MonitorSource.from_software(
            stack["software"], schema_rows=config_schema_rows)
        view = monitor_summary_payload(stack["monitor"].read())
        events = activity_records(source)[-60:][::-1]      # 近 60 条，新的在上
        config = config_tree(source)
        mode = getattr(stack["authority"].mode, "value", stack["authority"].mode)
        return render(request, "monitor.html", view=view, mode=str(mode),
                      events=events, config=config, stack_ready=True,
                      control_ready=control_ready)

    @app.post("/monitor/mode")
    def switch_write_mode(target: str = Form("locked"), token: str = Form("")):
        """人类侧切写权模式：**口令 + 服务端钉 actor=human**（两道都要）。

        ⚠ **边界（如实写，不假装在防）**：这个口令挡的是**本机其他进程**，
        **挡不住有权读你文件的 AI**（它能读到 `~/.paperpilot/control-token`）。
        它存在的意义是让"控制端点不是谁都能按"成立，**不是**"AI 不能自授权"——
        后者靠口令放在**仓外**（AI 的文件访问通常被限在工作区）来尽量成立。

        **fail-closed**：口令文件不存在 / 口令空 / 不匹配 ⇒ **403 + 可读错误**，
        **绝不静默放行**（控制端点没有"默认放开"这一档）。
        """
        if stack is None:
            return RedirectResponse("/monitor?msg=未接监控面", status_code=303)
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
                    "<p><a href='/monitor'>← 回到操作审计页</a></p>"
                    "</div>"),
            )

        from mecha.authority import Mode
        try:
            stack["authority"].switch_mode(Mode(target), side="human")
            msg = f"写权已切到 {target}"
        except Exception as exc:  # noqa: BLE001 - 展示可教学拒绝（含非法 target）
            msg = f"切换失败：{exc}"
        return RedirectResponse(f"/monitor?msg={quote(msg)}", status_code=303)

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

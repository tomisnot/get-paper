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
        s = container.settings
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
        return RedirectResponse("/settings?msg=全局参数已保存（AI provider 重启后生效）", status_code=303)

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

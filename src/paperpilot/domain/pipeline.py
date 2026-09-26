"""每日流水线编排：候选 → 硬规则 → 打分 → 配额筛选 → 精读 → 简报。

特性（DESIGN.md §6 / §17）：
- 幂等：同一天已有简报且未指定 force 时直接复用，不重复生成
- 可降级：AI 任一环节缺席/失败，自动落到本地兜底，流程照跑完
- **可被 AI 分段驱动**（M4，DSH/MCP 集成轨）：`prepare_review` 只做到「候选+规则+基线分」
  并把候选交给外部（DSH 里的 AI 或人）；`submit_review` 收回评审；
  `finalize_review` 用评审（缺的用基线分）完成筛选与组装。
  一键全流程仍是 `run()`（程序化 AI 档或 heuristic）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from ..infra.ai.errors import AIParseError  # noqa: F401  （re-export 便于调用方捕获）
from .models import (
    BriefingContent,
    BriefingItem,
    BriefingItemLite,
    BriefingStats,
    PaperSummary,
    RelevanceScore,
)
from .policy import (
    RuleGate,
    SelectionPolicy,
    extractive_summary,
    fallback_keyword_score,
    for_topic,
    select_for_briefing,
)

if TYPE_CHECKING:  # 领域层不运行时依赖 infra / config 实现
    from ..config import Settings
    from ..infra.orm import Paper, Topic
    from .ports.notify import Notifier
    from .ports.render import BriefRenderer
    from .ports.repo import PaperRepository

log = logging.getLogger("paperpilot.pipeline")

# 交给 AI 评审的候选上限（防 context 爆炸；回程体积纪律同 Energy Level I4）
_MAX_REVIEW_CANDIDATES = 40
_REVIEW_ABSTRACT_CHARS = 700

#: 合法标签的单一事实源（N9）：越界不静默降级，而是进 rejected。
_RELEVANT_LABELS = ("must_read", "worth", "skip")

#: 本次运行（当前线程）的配置覆盖：线程隔离，**不改共享 settings**。
#: 供 mecha 适配层把 Gate 里的配置态权威地作用于单次 Engine.run，而不污染
#: Web「立即运行」/scheduler/CLI 等并发线程读到的人类配置（settings.yaml）。
_CONFIG_OVERRIDE = threading.local()


@contextmanager
def pipeline_config_override(*, scoring=None, lookback_days: int | None = None):
    """在当前线程临时覆盖评分/回溯配置（None = 不覆盖）；退出即还原。

    线程局部 ⇒ 并发运行互不干扰（无锁、无共享可变状态）。覆盖对象应是
    ``ScoringCfg`` 的副本（如 ``settings.scoring.model_copy(update=…)``），
    调用方不要传共享实例再去改它。
    """
    prev_scoring = getattr(_CONFIG_OVERRIDE, "scoring", None)
    prev_lookback = getattr(_CONFIG_OVERRIDE, "lookback_days", None)
    _CONFIG_OVERRIDE.scoring = scoring
    _CONFIG_OVERRIDE.lookback_days = lookback_days
    try:
        yield
    finally:
        _CONFIG_OVERRIDE.scoring = prev_scoring
        _CONFIG_OVERRIDE.lookback_days = prev_lookback


@dataclass
class RunResult:
    run_id: str
    date: str
    briefing_id: int | None = None
    fetched: int = 0
    after_rules: int = 0
    selected: int = 0
    reused: bool = False
    degraded: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class PreparedReview:
    """prepare_review 的产物（同时落盘 data/reviews/{date}.json）。"""

    date: str
    run_id: str
    payload: dict


class DailyPipelineService:
    def __init__(
        self,
        repo: PaperRepository,
        *,
        settings: Settings,
        ranker=None,
        summarizer=None,
        renderer: BriefRenderer | None = None,
        notifier: Notifier | None = None,
        blocked_authors: Sequence[str] = (),
        ai_provider: str = "heuristic",
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.ranker = ranker
        self.summarizer = summarizer
        self.renderer = renderer
        self.notifier = notifier
        self.blocked_authors = blocked_authors
        self.ai_provider = ai_provider

    # ---- 生效配置（线程局部覆盖优先，否则回落共享 settings）----
    def _eff_scoring(self):
        """本次运行生效的评分配置：线程局部覆盖优先，无覆盖则用 settings.scoring。"""
        override = getattr(_CONFIG_OVERRIDE, "scoring", None)
        return self.settings.scoring if override is None else override

    def _eff_lookback_days(self) -> int:
        """本次运行生效的回溯天数：线程局部覆盖优先，无覆盖则用 settings。"""
        override = getattr(_CONFIG_OVERRIDE, "lookback_days", None)
        return self.settings.lookback_days if override is None else override

    # ================================================================== 一键全流程
    def run(
        self,
        when: date | None = None,
        *,
        force: bool = False,
        actor: str = "pipeline",
        reason: str = "",
    ) -> RunResult:
        """无外部评审的一键全流程（程序化 AI 档或 heuristic）。"""
        target = when or date.today()
        date_str = target.isoformat()
        run_id = uuid.uuid4().hex[:12]

        existing = self.repo.briefing_for_date(date_str)
        if existing is not None and not force:
            log.info("[%s] 简报已存在，跳过（--force 可重跑）", date_str)
            return RunResult(
                run_id=existing.run_id,
                date=date_str,
                briefing_id=existing.id,
                selected=len((existing.stats or {}).get("items", [])),
                reused=True,
            )

        self.repo.create_run(run_id, mode="daily")
        try:
            ranked, n_candidates, kept_ids, degraded, ai_ms = self._prepare_and_rank(
                run_id, actor=actor, reason=reason
            )
            return self._assemble(
                date_str, run_id, ranked, n_candidates, kept_ids,
                degraded=degraded, ai_ms=ai_ms, actor=actor, reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("流水线失败")
            self.repo.finish_run(run_id, status="failed", error=str(exc))
            return RunResult(run_id=run_id, date=date_str, error=str(exc))

    # ================================================================== 分段驱动（MCP/AI）
    def prepare_review(
        self, when: date | None = None, *, actor: str = "ai", reason: str = ""
    ) -> PreparedReview:
        """阶段 1-3：候选 → 硬规则 → 基线分（heuristic）。落 review 文件，等外部评审。"""
        target = when or date.today()
        date_str = target.isoformat()
        run_id = uuid.uuid4().hex[:12]
        self.repo.create_run(run_id, mode="review-prepare")

        ranked, n_candidates, kept_ids = self._prepare_stages(
            run_id, actor=actor, reason=reason
        )
        topics = {t.id: t for _, t, _ in ranked}

        # 一篇论文可能命中多个主题：只保留基线分最高的那个主题（评审面按论文去重）
        best: dict[str, tuple[Paper, Topic, RelevanceScore]] = {}
        for paper, topic, score in ranked:
            cur = best.get(paper.arxiv_id)
            if cur is None or score.score > cur[2].score:
                best[paper.arxiv_id] = (paper, topic, score)
        unique_ranked = sorted(best.values(), key=lambda x: -x[2].score)

        candidates = [
            {
                "arxiv_id": paper.arxiv_id,
                "title": paper.title,
                "primary_category": paper.primary_category,
                "topic": topic.name,
                "abstract": (paper.abstract or "")[:_REVIEW_ABSTRACT_CHARS],
                "baseline": score.model_dump(),
            }
            for paper, topic, score in unique_ranked[:_MAX_REVIEW_CANDIDATES]
        ]
        payload = {
            "date": date_str,
            "run_id": run_id,
            "n_candidates": n_candidates,
            "n_after_rules": len(kept_ids),
            "topics": [
                {
                    "id": t.id,
                    "name": t.name,
                    "description": t.description,
                    "keywords": list(t.keywords or []),
                }
                for t in topics.values()
            ],
            "candidates": candidates,
            "how_to_review": (
                "逐篇判断与用户研究兴趣的相关性：score∈[0,1]、label∈"
                "{must_read,worth,skip}、reason 用一句中文说明；对你想入选的篇目额外给 "
                "summary{tldr,problem,method,results,novelty,keywords}（只依据给定摘要，不得编造）。"
                "完成后调用 submit_review 一次性提交全部评审。"
            ),
        }
        self._save_review_file(date_str, run_id, unique_ranked)
        self.repo.finish_run(
            run_id,
            status="awaiting-review",
            stats={"n_candidates": n_candidates, "n_after_rules": len(kept_ids)},
        )
        return PreparedReview(date=date_str, run_id=run_id, payload=payload)

    def submit_review(self, date_str: str, reviews: Sequence[dict]) -> dict:
        """接收外部（AI/人）评审并落盘：校验 arxiv_id 必须在候选内、字段过 pydantic。"""
        review_path = self._review_path(date_str)
        if not review_path.exists():
            raise AIParseError(
                f"{date_str} 没有待评审的候选",
                hint="先调用 prepare_review(date) 生成候选清单",
            )
        stored = json.loads(review_path.read_text(encoding="utf-8"))
        by_id = {c["arxiv_id"]: c for c in stored["candidates"]}

        accepted, rejected = 0, []
        for idx, item in enumerate(reviews):
            # ⭐ per-item fail（N1）：坏苹果单独进 rejected，绝不击穿整批——一次手滑不等于整只手罢工。
            if not isinstance(item, dict):
                rejected.append({"arxiv_id": "", "why": f"第{idx}项不是对象，需 {{arxiv_id,score,label}}"})
                continue
            arxiv_id = str(item.get("arxiv_id", "")).strip()
            if not arxiv_id:
                rejected.append({"arxiv_id": "", "why": f"第{idx}项缺必填字段 arxiv_id"})
                continue
            if arxiv_id not in by_id:
                rejected.append({"arxiv_id": arxiv_id, "why": "不在本次候选清单内"})
                continue
            if item.get("score") is None:                # N1：缺 score 不再直接索引报 KeyError
                rejected.append({"arxiv_id": arxiv_id,
                                 "why": "缺必填字段 score（reviews 每项需 arxiv_id/score/label）"})
                continue
            label = str(item.get("label", "skip")).strip().lower()   # N9 归一化（尾空格/大小写）
            if label not in _RELEVANT_LABELS:
                rejected.append({"arxiv_id": arxiv_id,
                                 "why": f"label={item.get('label')!r} 非法，须为 {'/'.join(_RELEVANT_LABELS)}"})
                continue
            try:
                score = RelevanceScore(
                    score=float(item["score"]),
                    label=label,
                    reason=str(item.get("reason", "")),
                    tags=[str(t) for t in item.get("tags", [])][:5],
                )
            except (ValueError, TypeError, KeyError) as exc:   # KeyError 也纳入（N1 同类隐患）
                rejected.append({"arxiv_id": arxiv_id, "why": f"字段非法: {exc}"})
                continue
            by_id[arxiv_id]["ai"] = score.model_dump()
            raw_summary = item.get("summary")
            if isinstance(raw_summary, dict):
                try:
                    by_id[arxiv_id]["ai_summary"] = PaperSummary(
                        tldr=str(raw_summary.get("tldr", "")),
                        problem=str(raw_summary.get("problem", "")),
                        method=str(raw_summary.get("method", "")),
                        results=str(raw_summary.get("results", "")),
                        novelty=str(raw_summary.get("novelty", "")),
                        keywords=[str(k) for k in raw_summary.get("keywords", [])][:8],
                    ).model_dump()
                except (ValueError, TypeError):
                    pass  # 总结可选，脏了就忽略
            accepted += 1

        stored["status"] = "reviewed"
        review_path.write_text(
            json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "ok": True,
            "date": date_str,
            "accepted": accepted,
            "rejected": rejected,
            "note": "评审已受理；调用 finalize_briefing(date) 生成简报",
        }

    def finalize_review(
        self,
        when: date | None = None,
        *,
        force: bool = False,
        actor: str = "ai",
        reason: str = "",
    ) -> RunResult:
        """阶段 4-8：用已提交评审（缺的用基线分）做筛选、精读、组装、落库。"""
        target = when or date.today()
        date_str = target.isoformat()

        existing = self.repo.briefing_for_date(date_str)
        if existing is not None and not force:
            return RunResult(
                run_id=existing.run_id,
                date=date_str,
                briefing_id=existing.id,
                selected=len((existing.stats or {}).get("items", [])),
                reused=True,
            )

        review_path = self._review_path(date_str)
        if not review_path.exists():
            raise AIParseError(
                f"{date_str} 没有评审数据",
                hint="先 prepare_review(date) 再 submit_review(date, reviews)",
            )
        stored = json.loads(review_path.read_text(encoding="utf-8"))
        run_id = stored["run_id"]

        # 用存档的候选重建 ranked（AI 评审优先，缺失回落到基线分）
        papers = {p.arxiv_id: p for p in self.repo.recent_papers(limit=10_000)}
        topics = {t.id: t for t in self.repo.enabled_topics()}
        ranked = []
        ai_summaries: dict[str, PaperSummary] = {}
        for cand in stored["candidates"]:
            paper = papers.get(cand["arxiv_id"])
            topic = topics.get(cand.get("topic_id"))
            if paper is None or topic is None:
                continue
            raw = cand.get("ai") or cand["baseline"]
            score = RelevanceScore(**raw)
            # 评审跑的打分也进历史（详情页「打分历史」可见 AI 判了什么）
            self.repo.save_scores(
                run_id=run_id, paper=paper, topic_id=topic.id,
                score=score, model="dsh-review" if cand.get("ai") else "heuristic",
            )
            ranked.append((paper, topic, score))
            if cand.get("ai_summary"):
                ai_summaries[paper.arxiv_id] = PaperSummary(**cand["ai_summary"])

        kept_ids = {p.arxiv_id for p, _, _ in ranked}
        result = self._assemble(
            date_str, run_id, ranked, stored.get("n_candidates", len(ranked)), kept_ids,
            degraded=[], ai_ms=0, pre_summaries=ai_summaries, review_mode=True,
            actor=actor, reason=reason,
        )
        stored["status"] = "finalized"
        review_path.write_text(
            json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    def review_status(self, date_str: str) -> dict:
        path = self._review_path(date_str)
        if not path.exists():
            # N2：失败走**标准信封**（带 error{kind,message,hint}），不再裸 ok:false——
            # 否则过 MCP 桥后顶层状态被吞成“调用失败”，模型无从自纠。
            return {"ok": False, "date": date_str, "status": "none",
                    "error": {"kind": "no_review",
                              "message": f"{date_str} 没有待评审的候选",
                              "hint": "先调用 prepare_review(date) 生成候选清单再 submit_review"}}
        stored = json.loads(path.read_text(encoding="utf-8"))
        reviewed = sum(1 for c in stored["candidates"] if c.get("ai"))
        return {
            "ok": True,
            "date": date_str,
            "status": stored.get("status"),
            "candidates": len(stored["candidates"]),
            "reviewed": reviewed,
        }

    # ================================================================== 内部阶段
    def _prepare_stages(self, run_id: str, *, actor: str = "system", reason: str = ""):
        """候选 → 硬规则 → 基线分。返回 (ranked, n_candidates, kept_ids)。"""
        self.repo.sync_topics(self.settings.topics, actor=actor, reason=reason)

        candidates: list[tuple[Topic, list[Paper]]] = []
        for topic in self.repo.enabled_topics():
            papers = self.repo.candidates_for_topic(
                topic, lookback_days=self._eff_lookback_days()
            )
            if papers:
                candidates.append((topic, papers))
        candidate_ids = {p.arxiv_id for _, papers in candidates for p in papers}
        n_candidates = len(candidate_ids)

        gate = RuleGate(blocked_authors=self.blocked_authors)
        after_rules: list[tuple[Topic, list[Paper]]] = []
        for topic, papers in candidates:
            kept, _rejected = gate.apply(papers, topic)
            if kept:
                after_rules.append((topic, kept))
        kept_ids = {p.arxiv_id for _, kept in after_rules for p in kept}
        # 被所有主题都拒的论文才标记 rejected；任一主题放行则保留 new，等待末尾归档/入选
        for _topic, papers in candidates:
            for paper in papers:
                if paper.arxiv_id not in kept_ids:
                    self.repo.mark_status(
                        paper, "rejected", actor=actor, reason=reason or "规则过滤未通过"
                    )

        ranked: list[tuple[Paper, Topic, RelevanceScore]] = []
        for topic, papers in after_rules:
            for paper in papers:
                ranked.append((paper, topic, fallback_keyword_score(paper, topic)))
        return ranked, n_candidates, kept_ids

    def _prepare_and_rank(self, run_id: str, *, actor: str = "system", reason: str = ""):
        """run() 用：基线分之后，若配了程序化 ranker 则升级为 AI 打分。"""
        ranked, n_candidates, kept_ids = self._prepare_stages(
            run_id, actor=actor, reason=reason
        )
        degraded: list[str] = []
        ai_ms = 0
        if self.ranker is not None:
            upgraded: list[tuple[Paper, Topic, RelevanceScore]] = []
            by_topic: dict[int, list[Paper]] = {}
            for paper, topic, _score in ranked:
                by_topic.setdefault(topic.id, []).append(paper)
            topics = {t.id: t for _, t, _ in ranked}
            for topic_id, papers in by_topic.items():
                scores, elapsed, err = self._rank(papers, topics[topic_id], run_id)
                ai_ms += elapsed
                if err:
                    degraded.append(err)
                for paper, score in zip(papers, scores, strict=False):
                    self.repo.save_scores(
                        run_id=run_id, paper=paper, topic_id=topic_id,
                        score=score, model=self._model_name(),
                    )
                    upgraded.append((paper, topics[topic_id], score))
            ranked = upgraded
        return ranked, n_candidates, kept_ids, degraded, ai_ms

    def _model_name(self) -> str:
        for impl in (self.ranker, self.summarizer):
            name = getattr(impl, "name", None)
            if name:
                return str(name)
        return self.ai_provider

    def _assemble(
        self,
        date_str: str,
        run_id: str,
        ranked: list[tuple[Paper, Topic, RelevanceScore]],
        n_candidates: int,
        kept_ids: set[str],
        *,
        degraded: list[str],
        ai_ms: int,
        pre_summaries: dict[str, PaperSummary] | None = None,
        review_mode: bool = False,
        actor: str = "pipeline",
        reason: str = "",
    ) -> RunResult:
        pre_summaries = pre_summaries or {}
        degraded = list(degraded)

        # 4) 全局去重（同一篇取最高分主题）+ 配额筛选
        best: dict[str, tuple[Paper, Topic, RelevanceScore]] = {}
        for paper, topic, score in ranked:
            cur = best.get(paper.arxiv_id)
            if cur is None or score.score > cur[2].score:
                best[paper.arxiv_id] = (paper, topic, score)

        by_topic: dict[int, list[tuple[Paper, RelevanceScore]]] = {}
        topic_by_id = {t.id: t for _, t, _ in ranked}
        for paper, topic, score in best.values():
            by_topic.setdefault(topic.id, []).append((paper, score))

        scoring = self._eff_scoring()
        base_policy = SelectionPolicy(
            threshold=scoring.threshold,
            quota_per_topic=scoring.quota_per_topic,
            max_papers=scoring.max_papers,
            max_per_author=scoring.max_per_author,
            must_read_cap=scoring.must_read_cap,
        )
        selected: list[tuple[Paper, Topic, RelevanceScore]] = []
        archived: list[tuple[Paper, Topic, RelevanceScore]] = []
        for topic_id, items in by_topic.items():
            topic = topic_by_id[topic_id]
            sel, arc = select_for_briefing(items, for_topic(base_policy, topic))
            selected.extend((p, topic, s) for p, s in sel)
            archived.extend((p, topic, s) for p, s in arc)

        # 全局总量上限（超出部分降为存档）
        selected.sort(key=lambda x: -x[2].score)
        overflow = selected[base_policy.max_papers :]
        selected = selected[: base_policy.max_papers]
        archived.extend(overflow)

        # 5) 精读：review 模式用 AI 已交总结；其余走 summarizer / 抽取式兜底
        items, sum_ms, sum_errors = self._summarize(selected, run_id, pre_summaries)
        ai_ms += sum_ms
        degraded.extend(dict.fromkeys(sum_errors))

        # 6) 渲染 + 落库
        content = self._build_content(
            date_str, n_candidates, len(kept_ids), items, archived, degraded, ai_ms,
            review_mode=review_mode,
        )
        markdown = self.renderer.render(content) if self.renderer else _fallback_markdown(content)
        stats_payload = {
            "stats": content.stats.model_dump(),
            "items": [i.model_dump(mode="json") for i in content.selected],
            "archived": [a.model_dump(mode="json") for a in content.archived],
        }
        briefing = self.repo.save_briefing(
            date=date_str,
            run_id=run_id,
            title=f"arXiv 每日简报 · {date_str}",
            markdown=markdown,
            stats=stats_payload,
            ai_enabled=content.stats.ai_enabled,
            actor=actor,
            reason=reason or ("dsh-review" if review_mode else f"run {run_id}"),
        )

        # 7) 更新论文状态
        for paper, _topic, _score in selected:
            self.repo.mark_status(paper, "in_briefing", actor=actor, reason=reason or "入选简报")
        for paper, _topic, _score in archived:
            if paper.status == "new":
                self.repo.mark_status(paper, "archived", actor=actor, reason=reason or "过规则未入选")

        self.repo.finish_run(run_id, status="ok", stats=stats_payload)

        # 8) 通知（可选，失败不影响主流程）
        if self.notifier is not None and self.settings.notify.enabled:
            try:
                self.notifier.notify(
                    title=f"arXiv 每日简报 · {date_str}", markdown=markdown, date=date_str
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("通知发送失败: %s", exc)

        log.info(
            "[%s] 完成：候选 %d → 过规则 %d → 入选 %d（%s）",
            date_str, n_candidates, len(kept_ids), len(selected),
            "dsh-review" if review_mode else self.ai_provider,
        )
        return RunResult(
            run_id=run_id,
            date=date_str,
            briefing_id=briefing.id,
            fetched=n_candidates,
            after_rules=len(kept_ids),
            selected=len(selected),
            degraded=degraded,
        )

    def _rank(self, papers, topic, run_id):
        """程序化 AI 档打分（失败降级关键词兜底）。返回 (scores, elapsed_ms, error)。"""
        if self.ranker is None:
            return (
                [fallback_keyword_score(p, topic) for p in papers],
                0,
                "ranker 未配置，使用关键词兜底",
            )
        t0 = time.perf_counter()
        try:
            scores = self.ranker.score_batch(papers=papers, profile=topic, run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.perf_counter() - t0) * 1000)
            self.repo.log_ai_call(
                port="ranker", purpose="score_batch",
                model=getattr(self.ranker, "name", "ranker"),
                latency_ms=elapsed, ok=False, error=str(exc),
            )
            return (
                [fallback_keyword_score(p, topic) for p in papers],
                elapsed,
                f"ranker 失败（{exc}），使用关键词兜底",
            )
        elapsed = int((time.perf_counter() - t0) * 1000)
        if len(scores) != len(papers):
            self.repo.log_ai_call(
                port="ranker", purpose="score_batch",
                model=getattr(self.ranker, "name", "ranker"),
                latency_ms=elapsed, ok=False,
                error=f"length mismatch {len(scores)} != {len(papers)}",
            )
            return (
                [fallback_keyword_score(p, topic) for p in papers],
                elapsed,
                "ranker 返回长度不符，使用关键词兜底",
            )
        self.repo.log_ai_call(
            port="ranker", purpose="score_batch",
            model=getattr(self.ranker, "name", "ranker"),
            latency_ms=elapsed, ok=True,
        )
        return list(scores), elapsed, None

    def _summarize(
        self,
        selected,
        run_id: str,
        pre_summaries: dict[str, PaperSummary] | None = None,
    ):
        """返回 (items: dict[arxiv_id, dict], ai_ms, error_messages)。"""
        pre_summaries = pre_summaries or {}
        results: dict[str, dict] = {}
        errors: list[str] = []
        ai_ms = 0

        def work(item):
            paper, topic, score = item
            if paper.arxiv_id in pre_summaries:
                return paper.arxiv_id, {
                    "paper": paper, "topic": topic, "score": score,
                    "summary": pre_summaries[paper.arxiv_id], "ai_summary": True,
                    "latency_ms": 0, "tokens": 0,
                }, 0, None
            if self.summarizer is not None:
                t0 = time.perf_counter()
                try:
                    summary = self.summarizer.summarize(
                        paper=paper, profile=topic, run_id=run_id
                    )
                    latency = int((time.perf_counter() - t0) * 1000)
                    self.repo.save_summary(
                        run_id=run_id, paper=paper, summary=summary,
                        model=getattr(self.summarizer, "name", "summarizer"),
                        latency_ms=latency,
                    )
                    self.repo.log_ai_call(
                        port="summarizer", purpose="summarize",
                        model=getattr(self.summarizer, "name", "summarizer"),
                        latency_ms=latency, ok=True,
                    )
                    return paper.arxiv_id, {
                        "paper": paper, "topic": topic, "score": score,
                        "summary": summary, "ai_summary": True,
                        "latency_ms": latency, "tokens": 0,
                    }, latency, None
                except Exception as exc:  # noqa: BLE001
                    latency = int((time.perf_counter() - t0) * 1000)
                    self.repo.log_ai_call(
                        port="summarizer", purpose="summarize",
                        model=getattr(self.summarizer, "name", "summarizer"),
                        latency_ms=latency, ok=False, error=str(exc),
                    )
                    summary = extractive_summary(paper)
                    self.repo.save_summary(
                        run_id=run_id, paper=paper, summary=summary,
                        model="extractive-fallback",
                    )
                    return paper.arxiv_id, {
                        "paper": paper, "topic": topic, "score": score,
                        "summary": summary, "ai_summary": False,
                        "latency_ms": latency, "tokens": 0,
                    }, latency, f"summarizer 对 {paper.arxiv_id} 失败，已降级抽取式摘要"
            summary = extractive_summary(paper)
            return paper.arxiv_id, {
                "paper": paper, "topic": topic, "score": score,
                "summary": summary, "ai_summary": False,
                "latency_ms": 0, "tokens": 0,
            }, 0, None

        workers = max(1, int(self.settings.ai.max_concurrency))
        if selected:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for arxiv_id, payload, latency, err in pool.map(work, selected):
                    results[arxiv_id] = payload
                    ai_ms += latency
                    if err:
                        errors.append(err)
        return results, ai_ms, errors

    def _build_content(
        self, date_str, n_candidates, n_after_rules, items, archived, degraded, ai_ms,
        *, review_mode: bool = False,
    ):
        selected_items = []
        for _arxiv_id, payload in items.items():
            paper, score, summary = payload["paper"], payload["score"], payload["summary"]
            selected_items.append(
                BriefingItem(
                    arxiv_id=paper.arxiv_id,
                    title=paper.title,
                    authors=list(paper.authors or []),
                    categories=list(paper.categories or []),
                    primary_category=paper.primary_category or "",
                    pdf_url=paper.pdf_url or "",
                    abs_url=paper.abs_url or "",
                    published_at=paper.published_at,
                    score=score.score,
                    label=score.label,
                    reason=score.reason,
                    tags=list(score.tags or []),
                    summary=summary,
                    ai_summary=payload["ai_summary"],
                )
            )
        selected_items.sort(key=lambda i: -i.score)

        archived_items = [
            BriefingItemLite(
                arxiv_id=paper.arxiv_id,
                title=paper.title,
                primary_category=paper.primary_category or "",
                score=score.score,
                label=score.label,
                reason=score.reason,
            )
            for paper, _topic, score in sorted(archived, key=lambda x: -x[2].score)
        ]

        stats = BriefingStats(
            fetched=n_candidates,
            after_rules=n_after_rules,
            selected=len(selected_items),
            ai_enabled=review_mode or self.ranker is not None or self.summarizer is not None,
            ai_provider="dsh-review" if review_mode else self.ai_provider,
            ai_latency_ms=ai_ms,
            degraded=degraded,
        )
        return BriefingContent(
            date=date_str, stats=stats, selected=selected_items, archived=archived_items
        )

    # ================================================================== review 文件
    def _review_path(self, date_str: str) -> Path:
        directory = self.settings.data_dir / "reviews"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{date_str}.json"

    def _save_review_file(self, date_str: str, run_id: str, ranked) -> None:
        stored = {
            "date": date_str,
            "run_id": run_id,
            "status": "pending",
            "n_candidates": len({p.arxiv_id for p, _, _ in ranked}),
            "candidates": [
                {
                    "arxiv_id": paper.arxiv_id,
                    "topic_id": topic.id,
                    "topic": topic.name,
                    "baseline": score.model_dump(),
                    "ai": None,
                    "ai_summary": None,
                }
                for paper, topic, score in ranked
            ],
        }
        self._review_path(date_str).write_text(
            json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------- 辅助函数
def _fallback_markdown(content: BriefingContent) -> str:
    """renderer 缺席时的极简兜底（正常不会走到）。"""
    lines = [f"# {content.date} 简报（无渲染器）", ""]
    for item in content.selected:
        lines.append(f"- [{item.title}]({item.abs_url}) — {item.score:.2f} — {item.reason}")
    return "\n".join(lines)

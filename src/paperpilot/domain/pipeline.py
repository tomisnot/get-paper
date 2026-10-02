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
    InterestBrief,
    PaperSummary,
    RelevanceScore,
)
from .policy import (
    PoolScope,
    RuleGate,
    SelectionPolicy,
    extractive_summary,
    fallback_pool_score,
    select_for_briefing,
)
from .profile import brief_from_view, dimension_scales

if TYPE_CHECKING:  # 领域层不运行时依赖 infra / config 实现
    from ..config import Settings
    from ..infra.orm import Paper
    from .ports.notify import Notifier
    from .ports.render import BriefRenderer
    from .ports.repo import PaperRepository

log = logging.getLogger("paperpilot.pipeline")

# 交给 AI 评审的候选上限（防 context 爆炸；回程体积纪律同 Energy Level I4）
_MAX_REVIEW_CANDIDATES = 40
_REVIEW_ABSTRACT_CHARS = 700
#: W5 两阶段评审：brief 阶段的短摘长度（粗筛只看标题+短摘，省 ~2/3 评审 input）。
_BRIEF_ABSTRACT_CHARS = 300

#: 合法标签的单一事实源（N9）：越界不静默降级，而是进 rejected。
_RELEVANT_LABELS = ("must_read", "worth", "skip")

#: 本次运行（当前线程）的配置覆盖：线程隔离，**不改共享 settings**。
#: 供 mecha 适配层把 Gate 里的配置态权威地作用于单次 Engine.run，而不污染
#: Web「立即运行」/CLI 等并发线程读到的人类配置（settings.yaml）。
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
        self, when: date | None = None, *, actor: str = "ai", reason: str = "",
        requeue: bool = False, stage: str = "full", arxiv_ids: tuple[str, ...] = (),
    ) -> PreparedReview:
        """阶段 1-3：候选 → 硬规则 → 基线分（heuristic）。落 review 文件，等外部评审。

        `requeue=True`（再审回炉）：先把近 lookback 窗口内 archived/in_briefing 拉回
        new（走可逆的 `reset_statuses` 事件，undo 一键回退）再走正常流程。同一天想
        再跑评审协议不必等 arXiv 上新。⚠ requeue 会覆盖上一轮评审结果（下次 finalize
        以新一轮为准），旧简报行仍在库中。

        ⭐ 空池诊断（与 N1/N2 同族）：候选清单为空时不再静默 ok:true+空数组——
        payload 带 `_empty_diagnosis`（真相+出路），能力层据此回标准 err 信封；
        不写 review 文件（避免 submit 到不存在的清单）。"""
        target = when or date.today()
        date_str = target.isoformat()
        run_id = uuid.uuid4().hex[:12]
        self.repo.create_run(run_id, mode="review-prepare")
        requeue_flipped = 0
        if requeue:
            flipped_res = self.repo.reopen_by_status(
                from_statuses=("archived", "in_briefing"), to_status="new",
                within_days=self._eff_lookback_days(), actor=actor,
                reason=reason or "再审回炉（prepare_review requeue=True）",
            )
            requeue_flipped = int(flipped_res.get("flipped") or 0)

        ranked, n_candidates, kept_ids = self._prepare_stages(
            run_id, actor=actor, reason=reason
        )
        interest = self._interest()          # 评审者需要知道"用户在追什么"——现在是**画像**

        # 评审面按论文去重（同一篇只留一次；分数已是**画像池**算的，与主题无关）
        best: dict[str, tuple[Paper, RelevanceScore]] = {}
        for paper, score in ranked:
            cur = best.get(paper.arxiv_id)
            if cur is None or score.score > cur[1].score:
                best[paper.arxiv_id] = (paper, score)
        unique_ranked = sorted(best.values(), key=lambda x: -x[1].score)

        # ⭐ W4 评审 floor：低于 review_floor 的基线不进候选包（降评审 input；库里仍在，
        # 调低 floor/requeue 可再议）。n_after_rules 仍报全量，floor 单独回执。
        floor = float(getattr(self._eff_scoring(), "review_floor", 0.0) or 0.0)
        # ⭐ 显式意图 > 启发式预筛（N12/N14）：arxiv_ids 点名 ⇒ 不套 floor（点名要的
        # 被挡回就是静默丢）；brief 粗筛段也不套——载荷本就小，低分救援权留给评审者。
        # floor 只在「stage=full 直接全量评」时生效（那才是它的省 token 场景）。
        brief = stage == "brief"
        wanted = {x.strip() for x in arxiv_ids if x.strip()}
        skip_floor = brief or bool(wanted)
        above = (unique_ranked if skip_floor
                 else [x for x in unique_ranked if x[1].score >= floor])
        floor_applied = {"floor": floor, "applied": not skip_floor,
                         "kept": len(above), "total": len(unique_ranked)}
        # W5 两阶段：brief ⇒ 只给标题+短摘（粗筛）；full+arxiv_ids ⇒ 回执只装 shortlist
        # 的全文摘要（**review 文件仍存全量**，submit 按全量校验、finalize 行为不变）。
        cut = _BRIEF_ABSTRACT_CHARS if brief else _REVIEW_ABSTRACT_CHARS
        # ⚠ 每条候选**只带画像池给的东西**（score/label/reason），不带任何"主题归属"字段：
        # 主题标签曾在这里漏给 AI（无人消费、纯泄漏），会被带回"按主题想"。见 PRINCIPLES v1.7。
        candidates = [
            {
                "arxiv_id": paper.arxiv_id,
                "title": paper.title,
                "primary_category": paper.primary_category,
                "abstract": (paper.abstract or "")[:cut],
                "baseline": score.model_dump(),
            }
            for paper, score in above[:_MAX_REVIEW_CANDIDATES]
        ]
        if wanted:
            view = [c for c in candidates if c["arxiv_id"] in wanted]
        else:
            view = candidates
        # ⭐ 空池诊断：不再静默 ok:true+candidates:[]；不写 review 文件。
        if not candidates:
            diagnosis = self._diagnose_empty_review(
                n_candidates=n_candidates, n_after_rules=len(kept_ids),
                requeue=requeue, requeue_flipped=requeue_flipped,
                pool_before_floor=len(unique_ranked), floor=floor,
            )
            self.repo.finish_run(
                run_id, status="empty",
                stats={"n_candidates": n_candidates, "n_after_rules": len(kept_ids),
                       "requeue_flipped": requeue_flipped},
            )
            return PreparedReview(
                date=date_str, run_id=run_id,
                payload={"date": date_str, "run_id": run_id, "stage": stage,
                         "_empty_diagnosis": diagnosis},
            )

        payload = {
            "date": date_str,
            "run_id": run_id,
            "n_candidates": n_candidates,
            "n_after_rules": len(kept_ids),
            "requeue_flipped": requeue_flipped,
            "floor_applied": floor_applied,
            # 主题池化前这里是"每个主题的 id/名/关键词表"；现在兴趣是一个**画像池**，
            # 所以给评审者的上下文就是画像摘要（主线分类/高频词/关注作者）。
            "interest": {
                "name": interest.name,
                "description": interest.description,
                "keywords": list(interest.keywords),
            },
            "candidates": view,
            "stage": stage,
            "how_to_review": (
                ("【粗筛】只看标题+短摘：挑出值得精评的 ≤15 篇，回 "
                 "prepare_review(date=…, stage='full', arxiv_ids='id1,id2,…') 拉全文摘要，"
                 "再逐篇精评后 submit_review（提交按全量候选校验，未评的回落基线分）。")
                if brief else
                "逐篇判断与用户研究兴趣的相关性：score∈[0,1]、label∈"
                "{must_read,worth,skip}、reason 用一句中文说明；对你想入选的篇目额外给 "
                "summary{tldr,problem,method,results,novelty,keywords}（只依据给定摘要，不得编造）。"
                + (f"本轮只精评 shortlist 内 {len(view)} 篇（其余存而未评，finalize 回落基线）。"
                   if wanted else "")
                + "完成后调用 submit_review 一次性提交全部评审。"
            ),
        }
        self._save_review_file(date_str, run_id, unique_ranked)
        self.repo.finish_run(
            run_id,
            status="awaiting-review",
            stats={"n_candidates": n_candidates, "n_after_rules": len(kept_ids)},
        )
        return PreparedReview(date=date_str, run_id=run_id, payload=payload)

    def _diagnose_empty_review(self, *, n_candidates: int, n_after_rules: int,
                               requeue: bool, requeue_flipped: int,
                               pool_before_floor: int = 0, floor: float = 0.0) -> dict:
        """空池诊断：四种"空"分开讲——(a) 有候选但全被硬规则拒；(b) 有但全在
        review_floor 之下（W4）；(c) 库里 0 篇 new（池已消费）；(d) 有 new 但窗口外。
        计数写进 reason 文本（err 信封只传 message/hint，数字得让模型看得见）。"""
        counts = dict(self.repo.counts_by_status() or {})
        n_new = int(counts.get("new", 0))
        n_arch = int(counts.get("archived", 0))
        n_brief = int(counts.get("in_briefing", 0))
        lookback = int(self._eff_lookback_days())
        tally = f"new={n_new}, archived={n_arch}, in_briefing={n_brief}"
        diag: dict[str, object] = {
            "kind": "empty_pool",
            "counts": {"new": n_new, "archived": n_arch, "in_briefing": n_brief},
            "lookback_days": lookback,
            "requeue_used": requeue, "requeue_flipped": requeue_flipped,
            "review_floor": floor,
        }
        if n_candidates > 0 and n_after_rules == 0:
            diag["reason"] = (f"{n_candidates} 篇 new 全被硬规则（黑名单作者/排除词/"
                              "分类）拒了，无一进评审。")
            diag["suggests"] = [
                "检查 /settings 的 blocked_authors 与主题 exclude_keywords",
                "放宽主题 categories（若过窄）",
            ]
        if ("reason" not in diag and n_after_rules > 0 and pool_before_floor > 0):
            diag["reason"] = (f"{n_after_rules} 篇过规则候选全部低于 review_floor={floor}"
                              f"（被 W4 滤空，计数 {tally}）。")
            diag["suggests"] = [
                "到 /settings 调低评审下限 scoring.review_floor（或改 0 全量放行）",
                "prepare_review(requeue=True) 把近 lookback 内 archived/in_briefing 原班人马拉回再审",
            ]
        elif "reason" not in diag and n_new == 0:
            tail = ("；requeue 已开但近 lookback 内无 archived 可拉回。"
                    if requeue else "；未启 requeue。")
            diag["reason"] = (f"库里 0 篇 new（池已消费：archived={n_arch}, "
                              f"in_briefing={n_brief}）" + tail)
            diag["suggests"] = [
                "prepare_review(requeue=True) 把近 lookback 内 archived/in_briefing 原班人马拉回再审",
                "fetch_papers(days=N) 拉 arXiv 新提交（当日也可能无新）",
            ]
        elif "reason" not in diag:
            diag["reason"] = (f"库里还有 {n_new} 篇 new，但都在 lookback_days={lookback}"
                              f" 窗口外（first_seen_at 太老；{tally}）。")
            diag["suggests"] = [
                f"到 /settings 放宽 lookback_days（当前 {lookback}）",
                "fetch_papers 抓最新提交",
                "prepare_review(requeue=True) 从近期 archived/in_briefing 回炉",
            ]
        diag["hint"] = "；".join(str(x) for x in diag["suggests"])
        return diag

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
        ranked = []
        ai_summaries: dict[str, PaperSummary] = {}
        for cand in stored["candidates"]:
            paper = papers.get(cand["arxiv_id"])
            if paper is None:
                continue
            raw = cand.get("ai") or cand["baseline"]
            score = RelevanceScore(**raw)
            # 评审跑的打分也进历史（详情页「打分历史」可见 AI 判了什么）。
            # `topic_id=None`：打分从此**只对画像池**，不再挂任何主题（老行保留历史值）。
            self.repo.save_scores(
                run_id=run_id, paper=paper, topic_id=None,
                score=score, model="dsh-review" if cand.get("ai") else "heuristic",
            )
            ranked.append((paper, score))
            if cand.get("ai_summary"):
                ai_summaries[paper.arxiv_id] = PaperSummary(**cand["ai_summary"])

        kept_ids = {p.arxiv_id for p, _ in ranked}
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
    #: 送给打分器的候选上限（按画像分排序取前 N）。防"某天入库暴涨 ⇒ AI 调用爆炸"；
    #: 240 ≈ 30 次批（batch=8），与旧口径（每主题各评一遍）同量级。
    _CANDIDATE_CAP = 240

    def _interest(self) -> InterestBrief:
        """当前**兴趣上下文**（画像池 → 给打分器/精读器）。日报线与推荐流共用这一个真相。"""
        return brief_from_view(self.repo.profile_view(), self.repo.profile_weights_map())

    def _prepare_stages(self, run_id: str, *, actor: str = "system", reason: str = ""):
        """候选 → 全局硬门 → **画像基线分**。返回 (ranked, n_candidates, kept_ids)。

        ⚠ 2026-09-30 主题池化：候选不再"按主题分类取"、门不再"每主题一套"、分不再
        "按主题关键词算"。现在是：**new + 回溯窗** 取候选 → 全局客观门（非英文/作者黑名单）
        → 画像池打分。`ranked` 的 ``topic`` 元素**只为显示**（可能缺省）。
        """
        self.repo.sync_topics(self.settings.topics, actor=actor, reason=reason)
        self.repo.sync_topic_pool(self.settings.topics, actor=actor, reason=reason)

        weights = self.repo.profile_weights_map()
        scales = dimension_scales(weights)      # 一次算好、整批复用（分母只取池子）
        rows = self.repo.pipeline_candidates(lookback_days=self._eff_lookback_days())
        n_candidates = len(rows)

        # 全局硬门只留"客观不可入"。**绝不能拿 `settings.arxiv_categories` 当门**：那是
        # 抓取范围（示例配置是 cs.*，而主题可能在 quant-ph），当门会把整个主题域一刀切掉。
        # 分类与排除词都改走画像权重（软影响）——这正是"不再死板"的落点。
        gate = RuleGate(blocked_authors=self.blocked_authors)
        kept, rejected = gate.apply(rows, PoolScope())
        for paper in rejected:
            self.repo.mark_status(paper, "rejected", actor=actor,
                                  reason=reason or "全局规则未通过")
        kept_ids = {p.arxiv_id for p in kept}

        # ⚠ **选择改走"前 N"（B 方案）**：新尺度（三维占比平均）下绝对值很小（实测最高 0.25、
        # 中位 0.07），任何"绝对阈值"都会要么全收要么全丢 ⇒ 排序取前 `_CANDIDATE_CAP` 才稳。
        scored = [(p, fallback_pool_score(p, weights, scales=scales)) for p in kept]
        scored.sort(key=lambda ps: (-ps[1].score, ps[0].arxiv_id))   # 平分时按 id，确定性
        top = scored[: self._CANDIDATE_CAP]
        # ⚠ 三元组里的 `topic` 槽位已**退场**（2026-09-30 A 方案）：它当年只为给评审者一个
        # "归属主题"标签，而那个标签无人消费、还会把 AI 带回"按主题想"。现在打分/排序/上下文
        # 全由画像池决定，候选就是 `(论文, 画像分)` —— 数据形状与机制一致。
        ranked: list[tuple[Paper, RelevanceScore]] = list(top)
        return ranked, n_candidates, kept_ids

    def _prepare_and_rank(self, run_id: str, *, actor: str = "system", reason: str = ""):
        """run() 用：基线分之后，若配了程序化 ranker 则升级为 AI 打分（**一次全局评**）。"""
        ranked, n_candidates, kept_ids = self._prepare_stages(
            run_id, actor=actor, reason=reason
        )
        degraded: list[str] = []
        ai_ms = 0
        if self.ranker is not None and ranked:
            papers = [p for p, _s in ranked]
            interest = self._interest()
            scores, elapsed, err = self._rank(papers, interest, run_id)
            ai_ms += elapsed
            if err:
                degraded.append(err)
            upgraded: list[tuple[Paper, RelevanceScore]] = []
            for (paper, _base), score in zip(ranked, scores, strict=False):
                self.repo.save_scores(
                    run_id=run_id, paper=paper,
                    topic_id=None,          # 打分只对画像池，不再挂主题（老行保留历史值）
                    score=score, model=self._model_name(),
                )
                upgraded.append((paper, score))
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
        ranked: list[tuple[Paper, RelevanceScore]],
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

        # 4) 全局去重（同一篇取最高分）＋ **一次全局选择**
        # 主题池化前：按 topic_id 分组，各套 `for_topic()` 的 threshold/quota。
        # 池化后：**没有"每主题"这个概念了**——兴趣是一个池子，选择是一次全局排序。
        best: dict[str, tuple[Paper, RelevanceScore]] = {}
        for paper, score in ranked:
            cur = best.get(paper.arxiv_id)
            if cur is None or score.score > cur[1].score:
                best[paper.arxiv_id] = (paper, score)
        # **零命中不入选**（用户口径 B：选择走"前 N"）：新尺度下"画像完全认不出"就是 0 分，
        # 让它们被"前 N"顺手灌进简报＝往简报里塞噪声。
        # ⚠ 它们必须**归档**而不是留在 new：状态机要"走完一遍判定"，否则会永远停在候选池里
        #   （实测漏过一次：`new` 残留 1 篇）。要捞回用 `requeue`。
        zeros = [(p, s) for p, s in best.values() if s.score <= 0]
        best = {k: v for k, v in best.items() if v[1].score > 0}

        scoring = self._eff_scoring()
        base_policy = SelectionPolicy(
            # ⚠ **阈值不再当门（B 方案）**：打分尺度换成"三维占比平均"后绝对值很小
            # （实测最高 0.25、中位 0.07）⇒ 老配置里 0.5/0.6 这类**绝对**阈值会把一切挡在门外
            # （实测：测试夹具的 threshold=0.5 让 selected 直接变 0）。选择改由
            # **总量前 N**（`max_papers`）＋ `must_read_cap` ＋ `max_per_author` 决定；
            # `scoring.threshold` 只保留在配置里读得懂老文件，**不再参与选择**。
            threshold=0.0,
            # "每主题配额"已废除 ⇒ 借用它当**总配额**（与 max_papers 同值，双保险）
            quota_per_topic=scoring.max_papers,
            max_papers=scoring.max_papers,
            max_per_author=scoring.max_per_author,
            must_read_cap=scoring.must_read_cap,
        )
        picked, _dropped = select_for_briefing(list(best.values()), base_policy)
        selected_ids = {p.arxiv_id for p, _s in picked}
        selected: list[tuple[Paper, RelevanceScore]] = [
            (p, s) for p, s in best.values() if p.arxiv_id in selected_ids
        ]
        selected.sort(key=lambda x: -x[1].score)
        # **硬总量上限**：`select_for_briefing` 里 must_read **不占配额**（那是有意的：必读不该
        # 被配额挤掉），所以光靠 quota 管不住总量 ⇒ 这里再按 max_papers 截断，超出者降为存档。
        # ⚠ 这一步旧实现就有，重写成全局选择时**弄丢过一次**（判据当场抓到：max_papers=2 却选了 3）。
        overflow = selected[base_policy.max_papers:]
        if overflow:
            selected = selected[: base_policy.max_papers]
        archived: list[tuple[Paper, RelevanceScore]] = [
            (p, s) for p, s in best.values() if p.arxiv_id not in selected_ids
        ] + list(overflow) + zeros

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

        def skeleton(item) -> dict:
            """快照**只钉骨架**：卡片（summary）**不进快照**，渲染时现 join。

            与 `FeedIssue` 同一口径（那边注释写着"快照只钉哪些篇/什么序/为什么，中文摘要等
            富化字段渲染时现 join"）。**为什么这条是结构性的**：摘要一旦被快照抄一份，
            写入端漏写事实源就**看不出来**——实测正是如此（评审路线没落库 ⇒ 简报有卡、
            详情页 `/feed` `read_paper` 全空，42 篇中招）。抄副本这条路拿掉之后，
            "简报有卡而表里没有"**在结构上不可能出现**：四处读同一张表，要空一起空。
            """
            data = item.model_dump(mode="json")
            data.pop("summary", None)
            return data

        stats_payload = {
            "stats": content.stats.model_dump(),
            "items": [skeleton(i) for i in content.selected],
            "archived": [skeleton(a) for a in content.archived],
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
        for paper, _score in selected:
            self.repo.mark_status(paper, "in_briefing", actor=actor, reason=reason or "入选简报")
        for paper, _score in archived:
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

    def _rank(self, papers, interest, run_id):
        """程序化 AI 档打分（失败降级**画像兜底**）。返回 (scores, elapsed_ms, error)。"""
        if self.ranker is None:
            return (
                [fallback_pool_score(p, interest.weights) for p in papers],
                0,
                "ranker 未配置，使用画像兜底",
            )
        t0 = time.perf_counter()
        try:
            scores = self.ranker.score_batch(papers=papers, interest=interest, run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.perf_counter() - t0) * 1000)
            self.repo.log_ai_call(
                port="ranker", purpose="score_batch",
                model=getattr(self.ranker, "name", "ranker"),
                latency_ms=elapsed, ok=False, error=str(exc),
            )
            return (
                [fallback_pool_score(p, interest.weights) for p in papers],
                elapsed,
                f"ranker 失败（{exc}），使用画像兜底",
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
                [fallback_pool_score(p, interest.weights) for p in papers],
                elapsed,
                "ranker 返回长度不符，使用画像兜底",
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
        interest = self._interest()          # 精读同样基于**画像**（不再是某个主题）

        def work(item):
            paper, score = item

            def entry(summary: PaperSummary, model: str, latency: int = 0) -> PaperSummary:
                """**摘要进入世界的唯一入口**：先落库，再交出去。

                为什么做成入口、而不是"在每个分支记得调 save_summary"：评审路线（`submit_review`
                → 这里）曾经**在内存里拿着摘要就 return**，于是卡片只写进了简报快照、**没进
                `paper_summaries` 事实源** ⇒ 详情页 / `/feed` / `read_paper` 全读不到（实测 42 篇
                论文中招）。让"手里有摘要但没落库"这种状态**不可表示**，比多写判据管用。
                """
                self.repo.save_summary(run_id=run_id, paper=paper, summary=summary,
                                       model=model, latency_ms=latency)
                return summary

            if paper.arxiv_id in pre_summaries:
                summary = entry(pre_summaries[paper.arxiv_id], "dsh-review")
                return paper.arxiv_id, {
                    "paper": paper, "score": score,
                    "summary": summary, "ai_summary": True,
                    "latency_ms": 0, "tokens": 0,
                }, 0, None
            if self.summarizer is not None:
                t0 = time.perf_counter()
                try:
                    summary = self.summarizer.summarize(
                        paper=paper, interest=interest, run_id=run_id
                    )
                    latency = int((time.perf_counter() - t0) * 1000)
                    entry(summary, getattr(self.summarizer, "name", "summarizer"), latency)
                    self.repo.log_ai_call(
                        port="summarizer", purpose="summarize",
                        model=getattr(self.summarizer, "name", "summarizer"),
                        latency_ms=latency, ok=True,
                    )
                    return paper.arxiv_id, {
                        "paper": paper, "score": score,
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
                    summary = entry(extractive_summary(paper), "extractive-fallback", latency)
                    return paper.arxiv_id, {
                        "paper": paper, "score": score,
                        "summary": summary, "ai_summary": False,
                        "latency_ms": latency, "tokens": 0,
                    }, latency, f"summarizer 对 {paper.arxiv_id} 失败，已降级抽取式摘要"
            summary = entry(extractive_summary(paper), "extractive-fallback")
            return paper.arxiv_id, {
                "paper": paper, "score": score,
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
            for paper, score in sorted(archived, key=lambda x: -x[1].score)
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
            "n_candidates": len({p.arxiv_id for p, _ in ranked}),
            # ⚠ 候选**不再带 topic/topic_id**（A 方案 2026-09-30）：给 AI 看的与存下来的
            # 都只有画像池给的东西。老文件里那两个键留着也无害——`submit_review` 已不读它们。
            "candidates": [
                {
                    "arxiv_id": paper.arxiv_id,
                    "baseline": score.model_dump(),
                    "ai": None,
                    "ai_summary": None,
                }
                for paper, score in ranked
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

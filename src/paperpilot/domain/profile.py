"""画像特征与打分（M1 · 纯函数域层：零 IO、确定性、可解释）。

设计律（feed 设计 §3.4，内部记录未随仓发布）：打分=权重线性命中，**why 必须产出**——
拿不出 why 的条目禁止进 feed（宁窄勿玄）。
"""

from __future__ import annotations

import re

from .models import InterestBrief

#: 小写词表级别的英文停用词（宁缺勿滥：这里只求"别把 the 当兴趣"，不是语言学工程）。
_STOP = frozenset(
    "the of and for with using based study studies via toward into over under between "
    "that this these those from on in to a an is are be we our their its can may also "
    "more most than then when while where which who whom whose about within without".split()
)
_WORD_RE = re.compile(r"[a-z][a-z\-]{4,}")


def title_terms(title: str, abstract: str = "", *, max_terms: int = 8) -> list[str]:
    """标题(+摘要头)抽词 → 小写、去停用、保序去重。词面=画像 term 维的键。"""
    text = f"{title} {abstract[:400]}".lower()
    out: list[str] = []
    for w in _WORD_RE.findall(text):
        if w in _STOP or w in out:
            continue
        out.append(w)
        if len(out) >= max_terms:
            break
    return out


def paper_features(categories: list[str], primary: str, title: str,
                   abstract: str = "", authors: list[str] | None = None) -> dict:
    """论文 → 画像可吃的特征（categories/primary/terms/authors/**text**）。

    ``text`` 是给**短语命中**用的原文（小写 title + abstract）——见 :func:`score_paper`。
    """
    return {
        "categories": list(categories or []),
        "primary": primary or (categories[0] if categories else ""),
        "terms": title_terms(title, abstract),
        "authors": [a for a in (authors or []) if a][:5],
        "text": f"{title or ''} {abstract or ''}".lower(),
    }


def pool_relevance(raw: float) -> float:
    """画像原始分 → **0~1 观感分**（``RelevanceScore`` 的 score 有 0..1 约束）。

    ``raw/(1+raw)``：单调、零参数、raw=0→0、raw=1→0.5、raw=3→0.75。负分（被排除词拉低）
    归零。**明码可调**——要更陡就把分母变小。
    """
    if raw <= 0:
        return 0.0
    return round(raw / (1.0 + raw), 4)


def label_for(relevance: float) -> str:
    """观感分 → 档位（与 `policy.fallback_keyword_score` 同一套切分，便于人理解）。"""
    if relevance >= 0.78:
        return "must_read"
    return "worth" if relevance >= 0.5 else "skip"


def brief_from_view(view: dict, weights: dict[tuple[str, str], float],
                    *, max_terms: int = 16) -> InterestBrief:
    """把画像读数（`repo.profile_view()`）＋权重表组装成给打分器的「兴趣上下文」。"""
    cats = [k for k, w, _h in view.get("top", {}).get("category", []) if w > 0][:8]
    terms = [k for k, w, _h in view.get("top", {}).get("term", []) if w > 0][:max_terms]
    auths = [k for k, w, _h in view.get("top", {}).get("author", []) if w > 0][:8]
    desc = (f"主线分类：{('、'.join(cats)) or '（暂无）'}；"
            f"高频词：{('、'.join(terms)) or '（暂无）'}；"
            f"关注作者：{('、'.join(auths)) or '（暂无）'}")
    return InterestBrief(description=desc, keywords=[*cats, *terms], weights=dict(weights))


def score_paper(feat: dict, weights: dict[tuple[str, str], float]
                ) -> tuple[float, list[str]]:
    """线性加权命中打分 + 人话 why。weights 键=(kind,key)：category/term/author。

    系数（**具体性越高越重**，全是明码供调）：主分类 1.0、副分类 0.6、**短语 0.8**、
    单词 0.5、作者 0.8。

    ⚠ 短语为什么单独一条路（2026-09-30）：主题关键词天然是**短语**（"neutral atom"、
    "berry phase"），而 ``title_terms`` 只抽单词 ⇒ 那些键**永远命不中**（实测 200 个画像键里
    22 个是这样白占位，而同一批词在日报线却活得好好的）。现在含空格的键走**子串命中**，
    与日报线的 ``matched_keywords`` **同一套语义** ⇒ 两条线共用一份词表，不再"同一个词两条命运"。
    """
    score = 0.0
    why: list[str] = []
    for c in feat["categories"][:4]:
        w = weights.get(("category", c), 0.0)
        if not w:
            continue
        coef = 1.0 if c == feat["primary"] else 0.6
        score += coef * w
        why.append(f"分类 {c} 画像权重 {w:+.2f}")
    for t in feat["terms"]:
        w = weights.get(("term", t), 0.0)
        if w:
            score += 0.5 * w
            why.append(f"词面命中 “{t}”（{w:+.2f}）")
    text = feat.get("text") or ""
    if text:
        for (kind, key), w in weights.items():
            if kind != "term" or not w or " " not in key:
                continue
            if key in text:                      # 与日报线 matched_keywords 同一套子串语义
                score += 0.8 * w
                why.append(f"短语命中 “{key}”（{w:+.2f}）")
    for a in feat["authors"]:
        w = weights.get(("author", a), 0.0)
        if w:
            score += 0.8 * w
            why.append(f"关注作者 {a}（{w:+.2f}）")
    return score, why

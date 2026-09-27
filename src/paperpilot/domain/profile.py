"""画像特征与打分（M1 · 纯函数域层：零 IO、确定性、可解释）。

设计律（FEED-DESIGN §3.4）：打分=权重线性命中，**why 必须产出**——
拿不出 why 的条目禁止进 feed（宁窄勿玄）。
"""

from __future__ import annotations

import re

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
    """论文 → 画像可吃的特征字典（categories/primary/terms/authors）。"""
    return {
        "categories": list(categories or []),
        "primary": primary or (categories[0] if categories else ""),
        "terms": title_terms(title, abstract),
        "authors": [a for a in (authors or []) if a][:5],
    }


def score_paper(feat: dict, weights: dict[tuple[str, str], float]
                ) -> tuple[float, list[str]]:
    """线性加权命中打分 + 人话 why。weights 键=(kind,key)：category/term/author。

    系数：主分类 1.0、副分类 0.6、term 0.5、author 0.8——全是明码，供调。
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
    for a in feat["authors"]:
        w = weights.get(("author", a), 0.0)
        if w:
            score += 0.8 * w
            why.append(f"关注作者 {a}（{w:+.2f}）")
    return score, why

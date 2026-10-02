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


def label_for(relevance: float) -> str:
    """观感分 → 档位。切分点是**照着真实库量出来的**，不是拍的。

    2026-09-30 换"三维占比"尺度后实测（636 篇）：最高 0.246 · p90 0.139 · p75 0.101 ·
    中位 0.072 · 9% 为 0。取 0.15 / 0.06 ⇒ must_read 7.2% · worth 46.4% · skip 46.4%
    —— 必读稀少、两头有区分，这才是 label 该有的样子。

    ⚠ 旧尺度（`raw/(1+raw)`）下同一批论文的中位是 **0.905、64% 被判 must_read**：饱和函数把
    档位彻底架空（真正在保护简报的是 must_read_cap 与总量闸，不是分数）。换尺度就是为了修它。
    """
    if relevance >= 0.15:
        return "must_read"
    return "worth" if relevance >= 0.06 else "skip"


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


def dimension_scales(weights: dict[tuple[str, str], float]) -> dict[str, float]:
    """每一维的**兴趣总质量**（该维正权重之和）——C 方案的归一化分母。

    为什么需要它（用户："词维度人多就称霸"）：词的键 117 个、分类只有 14 个，直接线性求和
    ⇒ **"人多"的维度天然主导**，不是因为它更重要。改用**维度内占比**替掉绝对量，维度之间才可比。

    ⚠ 分母只取决于**你自己的池子**（不取决于本次候选集）⇒ 同一篇论文换一批候选算出来**分一样**
    （这正是"分位归一"被用户否掉的原因：那个每跑一次都可能变，不可复现、不可审计）。
    """
    out = {"category": 0.0, "term": 0.0, "author": 0.0}
    for (kind, _key), w in (weights or {}).items():
        if w > 0:
            out[kind] = out.get(kind, 0.0) + float(w)
    return out


_KIND_CN = {"category": "分类", "term": "词面", "author": "作者"}


def score_paper(feat: dict, weights: dict[tuple[str, str], float],
                *, scales: dict[str, float] | None = None) -> tuple[float, list[str]]:
    """**三维占比**打分（C 方案）+ 人话 why。权重键=(kind,key)：category/term/author。

    每维占比 = (Σ 命中系数·w) ÷ 该维兴趣总质量；总分 = **三维占比的平均**，截到 0..1
    （`RelevanceScore` 有 0~1 约束）。

    系数（具体性越高越重，明码）：主分类 1.0、副分类 0.6、**短语 0.8**、单词 0.5、作者 0.8。

    换来三条性质：
    * **尺度无关**：池子整体放大 10 倍，分数不变（分子分母同比）⇒ 活跃度不再改变阈值含义；
    * **维度可比**：不再因为"词的键多"就由词主导；
    * **不依赖候选集**：分母只取你自己的池子 ⇒ 同一篇论文两次运行分一样（可复现、可审计）。

    含空格的键走**子串命中**（与日报线 `matched_keywords` 同一套语义），
    因此主题里的多词短语在两条线上都生效。
    """
    if scales is None:
        scales = dimension_scales(weights)

    parts: list[tuple[str, str, float]] = []      # (kind, 说明文案, 该键的命中量)
    for c in feat["categories"][:4]:
        w = weights.get(("category", c), 0.0)
        if not w:
            continue
        coef = 1.0 if c == feat["primary"] else 0.6
        parts.append(("category", f"分类 {c}", coef * w))
    for t in feat["terms"]:
        w = weights.get(("term", t), 0.0)
        if w:
            parts.append(("term", f"词面 “{t}”", 0.5 * w))
    text = feat.get("text") or ""
    if text:
        for (kind, key), w in weights.items():
            if kind != "term" or not w or " " not in key:
                continue
            if key in text:                       # 与日报线 matched_keywords 同一套子串语义
                parts.append(("term", f"短语 “{key}”", 0.8 * w))
    for a in feat["authors"]:
        w = weights.get(("author", a), 0.0)
        if w:
            parts.append(("author", f"关注作者 {a}", 0.8 * w))

    why: list[str] = []
    for kind, label, v in parts:
        den = scales.get(kind, 0.0)
        why.append(f"{label}（占{_KIND_CN[kind]}兴趣 {(v / den if den > 0 else 0.0):+.0%}）")

    total = 0.0
    for kind in ("category", "term", "author"):
        den = scales.get(kind, 0.0)
        if den > 0:
            num = sum(v for k, _l, v in parts if k == kind)
            total += num / den
    # **四舍五入到 4 位**：权重读数是按"此刻"算衰减的，两次调用差几微秒就会在末位抖
    # ⇒ 回执 JSON 的字节数会跟着变（实测：同一个评审载荷两次调用差 2 字节，判据红）。
    # 4 位远小于切分粒度（0.15/0.06），既稳又不损失判断力。
    return round(max(0.0, min(1.0, total / 3.0)), 4), why

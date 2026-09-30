"""画像池页面（/profile）的视图模型：把 `profile_weights` 变成一张**可读的体检单**。

与 network / graph_view 同构：**计算在 app 层，模板只负责画**。三件事必须在数据里说清，
否则这张表只是"一堆数字"：

1. **来源**（`source`）：`topic:<名>` = 人手建的主题包注入的先验；`signal` = 行为学出来的。
   这是"删主题只撤基线、不误伤学到的"那句话的前提，所以必须显示出来。
2. **可达性**：这个键**现在能不能命中论文**。含空格的键走**子串命中**（与日报线
   `matched_keywords` 同一套语义）；单词键要能被 `title_terms` 抽出来；作者键要与库内
   作者串**逐字一致**（差一个连字符就永久失效——实测：配置写 `Chaoyang Lu`、库里是
   `Chao-Yang Lu`）。不可达的键只是"暂未出现"，不是结构错误，但要让人看得见。
3. **短语命中篇数**：把"白占位"从形容词变成数字（改前 22 个短语全是 0 篇）。
"""

from __future__ import annotations

from ..domain.profile import title_terms
from ..infra import arxiv_taxonomy as tax

#: 每个维度显示的键数上限（页面别太长；其余折叠成一行提示）。
TOP_N = {"category": 40, "term": 30, "author": 25}
KIND_LABEL = {"category": "arXiv 分类", "term": "关键词（词面）", "author": "作者"}
#: 维度系数（与 `domain.profile.score_paper` 逐字对应，改那边要改这里）。
KIND_COEF = {"category": "主分类 1.0 / 副分类 0.6", "term": "单词 0.5 · 短语 0.8", "author": "0.8"}
#: 信号系数（与 `repo.SIGNAL_WEIGHTS` 对应；页面把它们印出来，省得人去翻代码）。
SIGNALS = (("view", 0.3), ("outbound", 0.5), ("download", 1.0), ("star", 0.8),
           ("read", 0.6), ("skip", -1.0), ("uninterested", -1.5))
POS_CAP = 0.3


def _corpus(repo) -> tuple[list[str], set[str], set[str], set[str]]:
    """可达性体检要的三样东西：每篇的正文（小写）、可抽出的词、作者串、分类。

    ⚠ 全库扫一遍是**故意的**：单机个人库（数百到数千篇）这点开销可以忽略，
    换来的是一张"哪些声明的键在空转"的真清单——比缓存复杂度和 stale 风险划算。
    """
    papers = repo.recent_papers(limit=100_000)
    texts: list[str] = []
    terms: set[str] = set()
    authors: set[str] = set()
    cats: set[str] = set()
    for p in papers:
        texts.append(f"{p.title or ''} {p.abstract or ''}".lower())
        terms.update(title_terms(p.title or "", p.abstract or ""))
        authors.update(a for a in (p.authors or []) if a)
        cats.update(c for c in (p.categories or []) if c)
        if p.primary_category:
            cats.add(p.primary_category)
    return texts, terms, authors, cats


def build(repo, *, top: int = 30, half_life_days: float = 30.0) -> dict:
    """编译 /profile 的视图模型（纯数据，模板不参与判断）。"""
    rows = repo.profile_rows(half_life_days=half_life_days)
    view = repo.profile_view(top=max(1, min(int(top), 50)), half_life_days=half_life_days)
    texts, terms, authors, cats = _corpus(repo)

    def phrase_hits(key: str) -> int:
        return sum(1 for t in texts if key in t)

    def verdict(row: dict) -> tuple[bool, str]:
        kind, key = row["kind"], row["key"]
        if kind == "term":
            if " " in key:                       # 短语：子串命中（与日报线同一套语义）
                return (phrase_hits(key) > 0,
                        "库里还没有论文的标题/摘要含这个短语（已接通，等它出现）")
            return (key in terms, "库里抽不出这个词（≥5 字符、非停用词才会被抽）")
        if kind == "author":
            return (key in authors, "与库内作者串不逐字一致（大小写/连字符/写法不同即失效）")
        if kind == "category":
            return (key in cats,
                    "库里暂无该分类的论文" if key in tax.KNOWN else "不是已知的 arXiv 分类")
        return True, ""

    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        live, why = verdict(r)
        r["live"], r["why_unseen"] = live, why
        r["from_topic"] = (r["source"] or "").startswith("topic:")
        r["topic_name"] = r["source"].split(":", 1)[1] if r["from_topic"] else ""
        r["phits"] = phrase_hits(r["key"]) if (r["kind"] == "term" and " " in r["key"]) else 0
        by_kind.setdefault(r["kind"], []).append(r)

    sections = []
    for kind in ("category", "term", "author"):
        lst = sorted(by_kind.get(kind, []), key=lambda x: -abs(x["decayed"]))
        shown = lst[:TOP_N[kind]]
        vmax = max((abs(x["decayed"]) for x in shown), default=1.0) or 1.0
        for x in shown:
            x["pct"] = round(min(100.0, abs(x["decayed"]) / vmax * 100.0), 1)
        sections.append({
            "kind": kind, "label": KIND_LABEL[kind], "coef": KIND_COEF[kind],
            "total": len(lst), "shown": shown, "omitted": max(0, len(lst) - len(shown)),
            "unseen": sum(1 for x in lst if not x["live"]),
            "neg": sum(1 for x in lst if x["decayed"] < 0),
        })

    phrases = [r for r in by_kind.get("term", []) if " " in r["key"]]
    learned = sum(1 for r in rows if not r["from_topic"])
    return {
        "half_life_days": half_life_days,
        "signals": SIGNALS, "pos_cap": POS_CAP,
        "total": len(rows), "learned": learned, "injected": len(rows) - learned,
        "unseen": sum(1 for r in rows if not r["live"]),
        "neg_rows": sum(1 for r in rows if r["decayed"] < 0),
        "phrases_total": len(phrases),
        "phrases_live": sum(1 for r in phrases if r["live"]),
        "entropy": view["category_entropy"],
        "total_hits": view["total_hits"],
        "sections": sections,
        "topics": sorted({r["topic_name"] for r in rows if r["topic_name"]}),
    }

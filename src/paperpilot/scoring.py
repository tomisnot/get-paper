"""兴趣画像：特征、权重、衰减、打分。

口径（docs/设计-总纲.md §7）：
- **信号是账本，权重是推导物**：`recompute` 从事件流重放，任何时候都能重建；
- 衰减是读时的，历史不动；
- 权重只用于排序，不参与"要不要抓"。
"""

from __future__ import annotations

import re
from collections import defaultdict

from . import db
from .repo import Repo, decayed

#: 三个维度的贡献占比。词面提权到 0.40：它是**意图**，分类只是**领域**。
#: 只有三个旋钮，别再加——机制一多，出问题就不知道是哪一条。
DIMENSION_ALPHA = {"category": 0.45, "term": 0.40, "author": 0.15}

_STOP = {
    "with", "from", "that", "this", "into", "using", "based", "toward", "towards",
    "under", "over", "between", "through", "their", "these", "those", "which",
    "quantum", "via", "for", "and", "the", "are", "can", "new", "two", "one",
    "high", "low", "large", "small", "novel", "efficient", "toward", "study",
    "using", "towards", "its", "has", "have", "not", "but", "all", "any",
}

#: **泛词黑名单**：单字键里这些词在所有论文里都出现，进画像等于给全库加同一个常数。
#: 只对**单词**生效（`optical tweezer array` 这种短语一律放行）。
#: 判据是"它能不能区分两篇论文"，不是"它重不重要"。
_GENERIC = {
    "model", "models", "theory", "theories", "system", "systems", "method",
    "methods", "approach", "approaches", "result", "results", "property",
    "properties", "simulation", "simulations", "study", "studies", "analysis",
    "measurement", "measurements", "experiment", "experiments", "scheme",
    "schemes", "protocol", "protocols", "framework", "frameworks", "effect",
    "effects", "physics", "paper", "work", "data", "state", "states",
    "light", "spatial", "atom", "atoms", "array", "arrays", "field", "fields",
    "control", "design", "device", "devices", "phase", "phases", "structure",
    "structures", "interaction", "interactions", "dynamic", "dynamics",
}
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-]{3,}")


def is_generic_term(key: str) -> bool:
    """单字泛词（`model` / `light` / `quantum`…）——不进画像，也不参与短语命中。

    两张表一起查：`_STOP`（虚词与无区分力的词）与 `_GENERIC`（领域泛词）。
    分开只是为了可读，判据是同一条：**它能不能区分两篇论文**。
    """
    words = str(key or "").lower().split()
    if len(words) != 1:
        return False
    word = words[0]
    return word in _STOP or word in _GENERIC or word.rstrip("s") in _GENERIC


def title_words(title: str) -> list[str]:
    out: list[str] = []
    for word in _WORD_RE.findall(title or ""):
        low = word.lower()
        if low in _STOP:
            continue
        if low not in out:
            out.append(low)
    return out[:12]


def paper_features(paper: dict, card: dict | None = None) -> dict:
    """一篇论文的特征。

    - `category` / `primary`：精确匹配 + **主/副区分**（主分类比顺带挂的副分类重要）；
    - `term`：**只取卡里的语义关键词**，且**滤掉单字泛词**（`model`/`light`/`array`…）——
      泛词在每篇里都出现，进画像等于给全库加常数，只会稀释真信号；
    - `text` / `title`：给打分时的**短语命中**用（纯代码，零 AI 成本）——
      没写卡的论文也能被"画像里的短语"命中，不再恒为 0。
    """
    categories = [c for c in (paper.get("categories") or []) if c]
    primary = paper.get("primary_category") or ""
    if primary and primary not in categories:
        categories.append(primary)
    terms = [t.get("canonical") if isinstance(t, dict) else str(t)
             for t in (card or {}).get("terms") or []]
    terms = [t for t in terms if t and not is_generic_term(t)]
    title = paper.get("title") or ""
    abstract = paper.get("abstract") or ""
    return {
        "category": categories,
        "primary": primary,
        "term": list(dict.fromkeys(terms)),
        "author": [a for a in (paper.get("authors") or []) if a],
        "title": title,
        "text": _norm_text(f"{title} {abstract}"),
    }


def profile_snapshot(repo: Repo, half_life_days: float, *,
                     now_iso: str | None = None) -> dict[str, dict]:
    """{维度: {键: {w, w_raw, hits, source}}}，`w` 是衰减后读数。"""
    snap: dict[str, dict] = {"category": {}, "term": {}, "author": {}}
    rows = repo.conn.execute("SELECT * FROM profile_weights").fetchall()
    for row in rows:
        kind = row["kind"]
        if kind not in snap:
            continue
        raw = float(row["w"])
        snap[kind][row["key"]] = {
            "w": decayed(raw, row["updated_at"], half_life_days, now_iso=now_iso),
            "w_raw": raw,
            "hits": int(row["hits"]),
            "source": row["source"],
        }
    return snap


def _max_weight(snapshot_dim: dict) -> float:
    """该维的最大正权重——归一化用**最大值**而不是总和。

    为什么换：除以总和时，一篇论文命中 1~3 个键除以几十上百个键，贡献被摊薄成常数
    （实测 25 条候选的分 0.157~0.353，why 清一色"分类 quant-ph +33%"）。
    除以最大值则是"你命中的最强键有强"——命中头号兴趣就是满分，符合直觉。
    """
    positive = [v["w"] for v in snapshot_dim.values() if v["w"] > 0]
    return max(1e-9, max(positive)) if positive else 1e-9


def _phrase_factor(key: str) -> float:
    """短语越长越可信：3 词及以上算满，2 词七折，单词四折。

    没有这一步，`array`/`atom` 这类泛词在摘要里到处命中，会把词面维灌成噪音。
    """
    words = len(str(key).split())
    if words >= 3:
        return 1.0
    return 0.7 if words == 2 else 0.4


def _norm_text(text: str) -> str:
    """短语匹配用的轻规范化：小写、连字符/斜线当空格、压空白。"""
    return re.sub(r"\s+", " ", (text or "").lower().replace("-", " ").replace("/", " ")).strip()


def score(features: dict, snapshot: dict[str, dict]) -> tuple[float, list[str]]:
    """返回 (0~1 分数, 理由列表)。

    两条机制，就这两条：
    1. **维度内除以最大值**（而不是总和）——命中头号兴趣即满分，不再被"维里键太多"摊薄；
    2. **词面维靠短语命中**——拿画像里已有的 term 键去扫这篇的标题+摘要（纯代码，零 AI 成本），
       不依赖"有没有写卡"，并按短语词数打折压住泛词。

    分类/作者本来就是精确匹配，不需要短语那条路。
    """
    total = 0.0
    reasons: list[tuple[float, str]] = []

    # ---- 分类 ----
    dim = snapshot.get("category") or {}
    if dim:
        # 主分类满权、副分类六折：投稿主分类才说明"这篇属于哪个领域"，
        # 顺带挂的 cross-list 分类只是"也沾一点"。
        primary = features.get("primary") or ""
        hits = [(k, dim[k]["w"] * (1.0 if k == primary else 0.6))
                for k in features.get("category") or []
                if k in dim and dim[k]["w"] > 0]
        if hits:
            hits.sort(key=lambda kv: -kv[1])
            share = min(1.0, sum(w for _, w in hits) / _max_weight(dim))
            total += DIMENSION_ALPHA["category"] * share
            key, weight = hits[0]
            reasons.append((DIMENSION_ALPHA["category"] * share,
                            f"分类 {key}（权重 {weight:.1f}）"))

    # ---- 词面：画像里的短语 × 这篇的文字 ----
    dim = snapshot.get("term") or {}
    text = features.get("text") or ""
    title_text = _norm_text(features.get("title", ""))
    if dim and text:
        ceiling = max((v["w"] * _phrase_factor(k) for k, v in dim.items() if v["w"] > 0),
                      default=0.0)
        hits: list[tuple[str, float, int, bool]] = []
        for key, value in dim.items():
            if value["w"] <= 0:
                continue
            needle = _norm_text(key)
            # 单词键不参与短语命中：卡里学出来的 "quantum"/"arrays" 这种泛词会在
            # 几乎每篇摘要里出现，命中它等于给全库加同一个常数——噪音，不是信号。
            if len(needle.split()) < 2:
                continue
            if needle and needle in text:
                in_title = needle in title_text
                # 同一个短语，写进标题比埋在摘要里更能说明"这篇就是讲这个"
                weight = value["w"] * _phrase_factor(key) * (1.0 if in_title else 0.6)
                hits.append((key, weight, len(needle.split()), in_title))
        if hits:
            hits.sort(key=lambda item: -item[1])
            share = min(1.0, sum(weight for _, weight, _, _ in hits) / max(1e-9, ceiling))
            total += DIMENSION_ALPHA["term"] * share
            key, weight, words, in_title = hits[0]
            reasons.append((DIMENSION_ALPHA["term"] * share,
                            f"短语「{key}」命中{'标题' if in_title else '摘要'}"
                            f"（{words} 词，权重 {weight:.1f}）"))

    # ---- 作者 ----
    dim = snapshot.get("author") or {}
    if dim:
        hits = [(k, dim[k]["w"]) for k in features.get("author") or []
                if k in dim and dim[k]["w"] > 0]
        if hits:
            hits.sort(key=lambda kv: -kv[1])
            share = min(1.0, sum(w for _, w in hits) / _max_weight(dim))
            total += DIMENSION_ALPHA["author"] * share
            key, weight = hits[0]
            reasons.append((DIMENSION_ALPHA["author"] * share,
                            f"作者 {key}（权重 {weight:.1f}）"))

    reasons.sort(key=lambda item: -item[0])
    return round(min(1.0, max(0.0, total)), 4), [text for _, text in reasons[:4]]


def rank(repo: Repo, papers: list[dict], half_life_days: float) -> list[dict]:
    """给一批论文打分并降序。返回 [{paper, score, why}]。"""
    snapshot = profile_snapshot(repo, half_life_days)
    cards = repo.cards_of([p["arxiv_id"] for p in papers])
    out = []
    for paper in papers:
        features = paper_features(paper, cards.get(paper["arxiv_id"]))
        value, why = score(features, snapshot)
        out.append({"paper": paper, "score": value, "why": why})
    out.sort(key=lambda item: (-item["score"], item["paper"]["arxiv_id"]))
    return out


def recompute_profile(repo: Repo, signal_weights: dict[str, float]) -> dict:
    """从事件流全量重建权重。锚点（source='anchor'）不动。"""
    weights: dict[tuple[str, str], float] = defaultdict(float)
    hits: dict[tuple[str, str], int] = defaultdict(int)
    last_ts: dict[tuple[str, str], str] = {}
    missing: list[str] = []
    signals = repo.signal_events()

    def _bump(slot: tuple[str, str], delta: float, ts: str) -> None:
        weights[slot] += delta
        hits[slot] += 1
        if ts > last_ts.get(slot, ""):
            last_ts[slot] = ts

    for signal in signals:
        weight = float(signal_weights.get(signal["signal"], 0.0))
        if weight == 0.0:
            continue
        paper = repo.paper(signal["target"])
        if paper is None:
            missing.append(signal["target"])
            continue
        features = paper_features(paper, repo.card(paper["id"]))
        # 特征不是等价的：主分类说明"这篇属于哪个领域"，副分类只是"也沾一点"，
        # 作者是品味（噪声更大）。三档系数，与打分侧的假设保持一致。
        primary = features.get("primary") or ""
        for key in features["category"]:
            _bump(("category", key), weight * (1.0 if key == primary else 0.6),
                  signal["ts"])
        for key in features["term"]:
            _bump(("term", key), weight, signal["ts"])
        for key in features["author"]:
            _bump(("author", key), weight * 0.8, signal["ts"])

    now = db.now_iso()
    repo.conn.execute("DELETE FROM profile_weights WHERE source != 'anchor'")
    for (dim, key), value in weights.items():
        repo.conn.execute(
            "INSERT INTO profile_weights (kind, key, w, source, hits, updated_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(kind, key) DO UPDATE SET"
            " w=excluded.w, hits=excluded.hits, updated_at=excluded.updated_at",
            (dim, key, value, "behavior", hits[(dim, key)], last_ts.get((dim, key), now)))
    repo.conn.commit()
    return {"signals": len(signals), "keys": len(weights),
            "missing_papers": sorted(set(missing))}


def set_anchor(repo: Repo, *, dimension: str, key: str, weight: float) -> None:
    if dimension not in DIMENSION_ALPHA:
        raise ValueError(f"未知维度 {dimension!r}")
    repo.conn.execute(
        "INSERT INTO profile_weights (kind, key, w, source, hits, updated_at)"
        " VALUES (?,?,?,?,0,?) ON CONFLICT(kind, key) DO UPDATE SET"
        " w=excluded.w, source='anchor', updated_at=excluded.updated_at",
        (dimension, key, float(weight), "anchor", db.now_iso()))
    repo.conn.commit()


def remove_anchor(repo: Repo, *, dimension: str, key: str) -> bool:
    cur = repo.conn.execute(
        "DELETE FROM profile_weights WHERE kind=? AND key=? AND source='anchor'",
        (dimension, key))
    repo.conn.commit()
    return cur.rowcount > 0


def profile_view(repo: Repo, settings, *, top: int = 12) -> dict:
    """画像池页要的东西：三维 top（带条形图百分比）、锚点、来源计数、熵。

    `pct` 是**该维内相对最大值**的百分比（不是占全部权重的比例）——条形图要看的是
    "这一维里谁比谁强多少"，跨维比长度没有意义（作者维的总量天然比分类维大得多）。
    """
    snap = profile_snapshot(repo, settings.half_life_days)
    view: dict = {"dimensions": {}, "entropy": {}, "signals": {}, "anchors": [],
                  "keys_total": {}, "half_life_days": settings.half_life_days}
    for dim, entries in snap.items():
        positive = [(k, v) for k, v in entries.items() if v["w"] > 0]
        positive.sort(key=lambda kv: -kv[1]["w"])
        ceiling = positive[0][1]["w"] if positive else 0.0
        rows = []
        for key, value in positive[:top]:
            rows.append({"key": key, "w": round(value["w"], 4),
                         "w_raw": round(value["w_raw"], 4), "hits": value["hits"],
                         "source": value["source"],
                         "pct": round(100.0 * value["w"] / ceiling, 1) if ceiling else 0.0})
        view["dimensions"][dim] = rows
        view["keys_total"][dim] = len(positive)
        total = sum(value["w"] for _, value in positive)
        if total > 0:
            import math
            entropy = -sum((v["w"] / total) * math.log(v["w"] / total)
                           for _, v in positive)
            view["entropy"][dim] = round(entropy, 3)
        else:
            view["entropy"][dim] = 0.0
    view["keys_sum"] = sum(view["keys_total"].values())
    rows = repo.conn.execute(
        "SELECT kind, key, w FROM profile_weights WHERE source='anchor'"
        " ORDER BY kind, key").fetchall()
    view["anchors"] = [{"dimension": r["kind"], "key": r["key"], "w": round(float(r["w"]), 4)}
                       for r in rows]
    counts = repo.conn.execute(
        "SELECT source, COUNT(*) c FROM profile_weights GROUP BY source").fetchall()
    view["signals"] = {r["source"]: r["c"] for r in counts}
    view["signal_counts"] = {
        r["op"].split(":", 1)[1]: r["c"] for r in repo.conn.execute(
            "SELECT op, COUNT(*) c FROM events WHERE op LIKE 'signal:%' GROUP BY op").fetchall()}
    return view

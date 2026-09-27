"""Feed 装配（M2 · 纯函数，确定性、0 token、0 IO）。

四道召回桶（capability 层分好）→ 道配额取数（同分类/同作者打散）→ 探索位每 4 插 1
交织。同一输入必得同一输出（可复算是设计要求，不是愿望）。
"""

from __future__ import annotations

MIX_PRESETS: dict[str, tuple[int, int, int, int]] = {
    "auto": (45, 25, 10, 20),
    "strict": (60, 20, 10, 10),
    "explorer": (30, 20, 10, 40),
}
EXPLORE_FLOOR_PCT = 10      # 探索硬地板（%）：任何 mix/quotas 不许压穿
ENTROPY_LOW_BITS = 1.0      # 分类熵低于此（≈集中在两个分类）⇒ 探索道自动加倍
MAX_PER_CATEGORY = 3
MAX_PER_AUTHOR = 1
LANES = ("primary", "adjacent", "hot", "explore")


def resolve_quotas(mix: str, quotas_csv: str) -> tuple[tuple[int, int, int, int], str]:
    """定四道配比：显式 csv > mix 预设 > auto。返回 (quotas, note)——被地板顶过要吱声。"""
    base = MIX_PRESETS.get((mix or "auto").strip(), MIX_PRESETS["auto"])
    note = ""
    raw = (quotas_csv or "").strip()
    if raw:
        try:
            parts = tuple(int(x) for x in raw.replace("，", ",").split(","))
        except ValueError:
            return base, "quotas 解析失败，已用 mix 预设"
        if len(parts) != 4 or any(p < 0 for p in parts) or sum(parts) <= 0:
            return base, "quotas 需 4 个非负整数且总和>0，已用 mix 预设"
        base = parts
    total = int(sum(base))
    if base[3] * 100 < total * EXPLORE_FLOOR_PCT:
        target = -(-(total * EXPLORE_FLOOR_PCT) // 100)       # ceil
        need = target - int(base[3])
        take = min(need, int(base[0]))
        base = (int(base[0]) - take, int(base[1]), int(base[2]), int(base[3]) + take)
        note = f"探索道低于 {EXPLORE_FLOOR_PCT}% 地板，已从主兴趣道挪 {take} 点过来"
    return tuple(int(x) for x in base), note


def maybe_entropy_boost(quotas: tuple[int, int, int, int], entropy: float
                        ) -> tuple[tuple[int, int, int, int], str]:
    """代码保底（信条 7）：画像收窄（熵低）时探索道加倍，AI 仍可再抬不可压穿。"""
    q = tuple(int(x) for x in quotas)
    if sum(q) > 0 and entropy < ENTROPY_LOW_BITS:
        half = max(1, q[3] // 2) if q[3] > 1 else 1
        take = min(half, q[0])
        if take:
            q = (q[0] - take, q[1], q[2], q[3] + take)
            return q, (f"分类熵 {entropy:.2f} < {ENTROPY_LOW_BITS} ⇒ 探索道自动加倍"
                       f"（+{take} 配额点，防茧房代码保底）")
    return q, ""


def allocate(buckets: dict[str, list[dict]], *, limit: int,
             quotas: tuple[int, int, int, int]) -> list[dict]:
    """按道配额取数（同分类≤3、同作者≤1），再交织：非探索每 4 条插 1 条探索。"""
    total_q = sum(quotas) or 100
    counts = [max(0, int(round(limit * q / total_q))) for q in quotas]
    picked: list[dict] = []
    picked_ids: set[str] = set()
    per_cat: dict[str, int] = {}
    per_author: dict[str, int] = {}

    def fits(it: dict) -> bool:
        if it["arxiv_id"] in picked_ids:
            return False
        if per_cat.get(it["primary_category"], 0) >= MAX_PER_CATEGORY:
            return False
        if any(per_author.get(a, 0) >= MAX_PER_AUTHOR for a in it["authors"]):
            return False
        return True

    def commit(it: dict, lane: str) -> None:
        it = {**it, "lane": lane}
        picked.append(it)
        picked_ids.add(it["arxiv_id"])
        per_cat[it["primary_category"]] = per_cat.get(it["primary_category"], 0) + 1
        for a in it["authors"]:
            per_author[a] = per_author.get(a, 0) + 1

    for lane, want in zip(LANES, counts, strict=True):
        got = 0
        for it in buckets.get(lane, []):
            if got >= want:
                break
            if fits(it):
                commit(it, lane)
                got += 1
    if len(picked) < limit:                     # 配额没吃饱 ⇒ 按道序轮转补满
        for lane in LANES:
            for it in buckets.get(lane, []):
                if len(picked) >= limit:
                    break
                if fits(it):
                    commit(it, lane)
    nonexp = [x for x in picked if x["lane"] != "explore"]
    exp = [x for x in picked if x["lane"] == "explore"]
    out: list[dict] = []
    while nonexp or exp:
        for _ in range(4):
            if nonexp:
                out.append(nonexp.pop(0))
        if exp:
            out.append(exp.pop(0))
    return out[:limit]

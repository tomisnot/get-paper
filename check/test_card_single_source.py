"""卡片**单一事实源**判据：快照不许内联摘要，卡片必须能从表里读到。

背景（实测事故）：评审路线（`submit_review` → `finalize_briefing`）产出的卡片曾经
**只写进简报快照的 JSON**、没进 `paper_summaries` ⇒ 简报页照样好看（它读自己那份副本），
而**详情页 / `/feed` / `read_paper` 全空**——42 篇论文中招，且删简报就永久丢卡。

修法不是"多写几条判据"，是**让那种状态不可表示**：
* 写入侧：摘要只能经由**单一入口**落库（`_summarize` 里那个 `entry()`）；
* 读取侧：快照**只钉骨架**（与 `FeedIssue` 同口径），卡片**渲染时现 join**。

于是"简报有卡而表里没有"在结构上不可能出现 ⇒ 本判据只钉这一条不变量（一条测试）。
"""

from __future__ import annotations

from tests.test_mecha_adapter import _call, _stack


def test_briefing_snapshot_has_no_summary_and_card_lives_in_the_table(tmp_path):
    """三件事一起验，都是同一条不变量的不同侧面：

    1. **快照里没有 summary**（内联副本已被拿掉——有人再抄回去，这里当场红）；
    2. **卡片在事实源里**（`paper_summaries` 读得到，评审路线也不例外）；
    3. **简报页仍能显示卡片**（现 join 生效——修完不能把功能也修没了）。
    """
    c, stack = _stack(tmp_path)
    tools = stack["tools"]

    prepared = _call(tools, "prepare_review")
    assert prepared["ok"] and prepared["candidates"]
    date = prepared["date"]
    reviews = [{"arxiv_id": x["arxiv_id"], "score": 0.8, "label": "worth",
                "reason": "判据", "summary": {"tldr": "单一事实源", "problem": "P",
                                              "method": "M", "results": "R",
                                              "novelty": "N", "keywords": ["k"]}}
               for x in prepared["candidates"]]
    assert _call(tools, "submit_review", date=date, reviews=reviews)["ok"]
    assert _call(tools, "finalize_briefing", date=date)["ok"]

    # ① 快照只钉骨架：不许出现 summary 字段（新老简报都不许再抄）
    digest = _call(tools, "read_digest", date=date, full=False)
    items = digest["items"]
    assert items, "简报没有条目，判据无从谈起"
    assert all("summary" not in it for it in items), "简报快照又内联摘要了"
    archived = (c.repo.briefing_for_date(date).stats or {}).get("archived") or []
    assert all("summary" not in it for it in archived), "归档项也内联了摘要"

    # ② 卡片落在事实源：每一条入选项都必须能从 paper_summaries 读到
    for it in items:
        card = c.repo.latest_summary(c.repo.get_paper(it["arxiv_id"]))
        assert card is not None and card.tldr == "单一事实源", (
            f"{it['arxiv_id']} 的卡片没进 paper_summaries（评审路线漏落库的老毛病）")

    # ③ 页面能显示：快照没抄，但渲染时 join 回来了
    from fastapi.testclient import TestClient

    from paperpilot.app.web import create_app

    page = TestClient(create_app(c, None), follow_redirects=True).get(f"/digest/{date}")
    assert page.status_code == 200
    assert "单一事实源" in page.text, "卡片在页面上消失了（现 join 没接上）"

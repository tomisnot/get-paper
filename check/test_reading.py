"""精读体系判据：消毒 / 块清单 / 锚定 / 批注 / 回执 / 阅读页 / 人侧删除。

能红的六类事故（都真实存在或极易发生）：

1. **把外部内容当可信内容**（脚本、`on*` 事件属性、`javascript:` 没剥）⇒ 断言消毒后什么都不剩；
2. **AI 手算字符偏移**（必然标错）⇒ 断言 quote→区间的偏移与原文**逐字对齐**；
3. **同名的另一处被标中**（论文里"这句话"常出现多次）⇒ 断言多命中报 `ambiguous` 并回候选；
4. **没有 id 的段落整段丢失**（LaTeXML 不保证每段都有 id）⇒ 断言补 id 后块完整、锚得住；
5. **删除批注流出给 AI** ⇒ 断言工具面没它、AI 通道被 scope 拒；
6. **阅读页把未归档/无 HTML 的论文当有正文渲染** ⇒ 断言如实给 404 与可教学文案。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from lxml import html as LH

from paperpilot.app.web import create_app
from paperpilot.capabilities import registry_for
from paperpilot.infra.arxiv import parse_atom
from paperpilot.infra.paperhtml import (
    ensure_block_ids,
    extract_blocks,
    locate_quote,
    outline,
    sanitize,
)

from .conftest import SAMPLE_XML, make_settings

#: 一份"像 LaTeXML 产出"的最小正文：有 id、有图、有一处**没有 id** 的段落，还有一个脚本。
FIXTURE = """<html><body>
<section id="S1"><h2>1 Introduction</h2>
<p id="S1.p1">Rydberg atoms enable programmable quantum simulation of gauge theories.</p>
<p>String breaking is a hallmark of confinement in gauge theories.</p>
</section>
<section id="S2"><h2>2 Method</h2>
<p id="S2.p1">We prepare a 51-atom array and quench the Hamiltonian.</p>
<figure id="S2.F1"><img src="fig1.png"><figcaption>Experimental setup</figcaption></figure>
<p id="S2.p2">String breaking is observed at late times.</p>
</section>
<script>alert('xss')</script>
<p id="S3.p1" onclick="steal()">Unsafe <a href="javascript:steal()">link</a></p>
</body></html>"""


def _paper(container, i: int = 0) -> str:
    papers = parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))
    container.repo.upsert_papers(papers)
    return papers[i].arxiv_id


def _archive(container, arxiv_id: str, html_text: str = FIXTURE, version: int = 1) -> None:
    """把一份正文"归档"成 fetch_paper_html 会落的样子（补 id + **入块级全文索引**两步）。"""
    root = LH.fromstring(html_text)
    sanitize(root)
    ensure_block_ids(root)
    clean = LH.tostring(root, encoding="unicode", method="html")
    base = container.settings.data_dir / "paper_html" / arxiv_id / f"v{version}"
    base.mkdir(parents=True, exist_ok=True)
    (base / "index.html").write_text(clean, encoding="utf-8")
    container.repo.index_paper_text(arxiv_id, version, extract_blocks(LH.fromstring(clean)))
    container.repo.save_paper_html(arxiv_id, version=version, source="arxiv",
                                   source_url=f"https://arxiv.org/html/{arxiv_id}v{version}",
                                   status="ok", sha256="deadbeef", bytes=len(clean),
                                   assets=0, asset_bytes=0, actor="ai", reason="判据")


def test_library_text_search_lands_on_the_block(tmp_path):
    """**与检索融合**：正文进块级全文索引 ⇒ 跨篇搜「原文在哪说」直接给到那一段。

    这是精读与全库检索的接缝：`search_papers` 答"哪篇相关"，`search_library_text` 答
    "原文在哪一句"——命中必须带 `arxiv_id` + 块 id，否则跳不到原文。
    """
    c = _container(tmp_path)
    aid = _paper(c)
    _archive(c, aid)
    reg = registry_for(c)
    assert c.repo.text_index_size() > 0

    hit = reg.invoke("search_library_text", q="hallmark of confinement")
    assert hit["ok"] and hit["count"] >= 1
    first = hit["hits"][0]
    assert first["arxiv_id"] == aid
    assert first["block"], "命中没带块 id ⇒ 跳不到原文（这条接缝就断了）"
    assert "confine" in first["snippet"].lower()

    assert reg.invoke("search_library_text", q="")["count"] == 0       # 空词不崩、不瞎给
    assert reg.invoke("search_library_text", q="绝不存在的词zzz")["count"] == 0


# ---------------------------------------------------------------- 消毒与块清单


def test_sanitize_strips_everything_executable():
    """外部内容按**敌意内容**处理：脚本、事件属性、javascript: 一律剥掉；id/文本原样保留。"""
    root = LH.fromstring(FIXTURE)
    killed = sanitize(root)
    out = LH.tostring(root, encoding="unicode", method="html")
    assert "<script" not in out and "alert" not in out
    assert "onclick" not in out and "javascript:" not in out
    assert killed["script"] >= 1 and killed["handlers"] >= 1 and killed["js_url"] >= 1
    assert 'id="S2.p2"' in out                      # 锚点语义不许被消毒破坏
    assert "String breaking is observed" in out


def test_blocks_cover_paragraphs_figures_and_sections_including_those_without_id():
    """块清单要**完整**：没 id 的段落由 `ensure_block_ids` 补 id 后照样入册。"""
    root = LH.fromstring(FIXTURE)
    assert ensure_block_ids(root) >= 1              # 那个没 id 的段落被补上了
    blocks = extract_blocks(root)
    ids = [b.block_id for b in blocks]
    kinds = {b.block_id: b.kind for b in blocks}
    assert "S1" in ids and kinds["S1"] == "section"
    assert kinds["S2.F1"] == "figure"
    assert any(b.block_id.startswith("pp-") for b in blocks), "无 id 的段落没被补进块清单"
    assert any("hallmark of confinement" in b.text for b in blocks)
    secs = outline(blocks)
    names = [s["section"] for s in secs]
    assert names[:2] == ["1 Introduction", "2 Method"]
    assert sum(s["blocks"] for s in secs) >= 4
    # 不属于任何节的块**如实归到「（无分节）」**（fixture 里 S3.p1 就在节外），
    # 而不是被静默丢掉——"没归到节里"必须是看得见的。
    assert "（无分节）" in names
    loose = next(s for s in secs if s["section"] == "（无分节）")
    assert loose["blocks"] >= 1


def test_locate_quote_is_exact_and_flags_ambiguity():
    """**AI 只给原句，区间由代码算**：唯一命中给精确偏移；多处命中必须报歧义（不许猜）。"""
    root = LH.fromstring(FIXTURE)
    ensure_block_ids(root)
    blocks = extract_blocks(root)

    hit = locate_quote(blocks, "We prepare a 51-atom array")
    assert hit.reason == "ok" and hit.candidates == 1
    a = hit.anchors[0]
    assert a.block == "S2.p1"
    blk = next(b for b in blocks if b.block_id == "S2.p1")
    assert blk.text[a.start:a.end] == "We prepare a 51-atom array"   # 逐字对齐
    assert a.prefix == "" and a.suffix.startswith(" and quench")

    amb = locate_quote(blocks, "String breaking")                    # 两处都有
    assert amb.reason == "ambiguous" and amb.candidates == 2
    assert {x.block for x in amb.anchors} == {"pp-1", "S2.p2"}

    one = locate_quote(blocks, "String breaking", block_id="S2.p2")  # 指定块 ⇒ 消歧
    assert one.reason == "ok" and one.anchors[0].block == "S2.p2"

    assert locate_quote(blocks, "这句话原文里没有").reason == "not_found"


# ---------------------------------------------------------------- 能力：读与标


def test_read_and_annotate_roundtrip(tmp_path):
    """读通道 → 标 → 回执：这条链是精读的核心闭环，一次跑通。"""
    c = _container(tmp_path)
    aid = _paper(c)
    _archive(c, aid)
    reg = registry_for(c)

    out = reg.invoke("read_paper_outline", arxiv_id=aid)
    assert out["ok"] and out["blocks"] >= 6
    assert out["sections"][0]["section"] == "1 Introduction"

    text = reg.invoke("read_paper_text", arxiv_id=aid, section="Method")
    got = {b["id"] for b in text["blocks"]}
    assert {"S2.p1", "S2.p2", "S2.F1"} <= got
    assert text["truncated"] is False

    found = reg.invoke("search_paper_text", arxiv_id=aid, q="quench")
    assert found["count"] == 1 and found["hits"][0]["block"] == "S2.p1"

    mark = reg.invoke("annotate_paper", arxiv_id=aid, quote="String breaking is observed",
                      block="S2.p2", body="**这就是本文主结果**", kind="note",
                      actor="ai", reason="判据")
    assert mark["ok"], mark
    assert "String breaking is observed" in mark["snippet"]      # 回执证明标在哪
    assert mark["mark"]["anchor"]["block"] == "S2.p2"

    ver = reg.invoke("verify_marks", arxiv_id=aid)
    assert ver["count"] == 1 and ver["unresolved"] == []
    assert ver["marks"][0]["resolved"] is True

    upd = reg.invoke("update_mark", mark_id=mark["mark"]["id"], body="改过了",
                     actor="ai", reason="判据")
    assert upd["ok"] and upd["mark"]["body"] == "改过了"
    res = reg.invoke("resolve_mark", mark_id=mark["mark"]["id"], actor="ai", reason="判据")
    assert res["ok"] and res["mark"]["status"] == "resolved"


def test_annotate_asks_for_block_when_ambiguous(tmp_path):
    """多处命中 ⇒ **回候选让 AI 指定**，而不是随便标一处（静默标错是最坏的结果）。"""
    c = _container(tmp_path)
    aid = _paper(c)
    _archive(c, aid)
    reg = registry_for(c)
    bad = reg.invoke("annotate_paper", arxiv_id=aid, quote="String breaking",
                     actor="ai", reason="判据")
    assert bad["ok"] is False and bad["error"]["kind"] == "ambiguous"
    assert len(bad["error"]["suggest"]) == 2
    miss = reg.invoke("annotate_paper", arxiv_id=aid, quote="原文里没有这句",
                      actor="ai", reason="判据")
    assert miss["ok"] is False and miss["error"]["kind"] == "not_found"


def test_read_paper_text_truncates_with_a_way_forward(tmp_path):
    """回程体积闸：截断必须**给续读游标**（静默丢内容＝bug）。

    注：`limit` 有 **500 字下限**（拒绝"一次只读 100 字"这种反效率用法），所以这里造一篇
    足够长的正文来触发截断。
    """
    long_doc = ("<html><body><section id='S1'><h2>1 Long</h2>"
                + "".join(f"<p id='S1.p{i}'>{'filler ' * 40}para {i}</p>" for i in range(1, 9))
                + "</section></body></html>")
    c = _container(tmp_path)
    aid = _paper(c)
    _archive(c, aid, html_text=long_doc)
    reg = registry_for(c)
    part = reg.invoke("read_paper_text", arxiv_id=aid, limit=600)
    assert part["ok"] and part["truncated"] is True and part["next_offset"] > 0
    rest = reg.invoke("read_paper_text", arxiv_id=aid, offset=part["next_offset"], limit=600)
    assert {b["id"] for b in part["blocks"]}.isdisjoint({b["id"] for b in rest["blocks"]})
    assert part["where"] and "续读" in part["where"]


def test_no_html_papers_are_told_apart_and_do_not_enter_the_system(tmp_path):
    """**没有 HTML 就不进体系**：如实报 no_html / 没归档，并给下一步（不回落 PDF）。"""
    c = _container(tmp_path)
    aid = _paper(c)
    reg = registry_for(c)
    never = reg.invoke("read_paper_outline", arxiv_id=aid)
    assert never["ok"] is False and never["error"]["kind"] == "no_html_archived"
    c.repo.save_paper_html(aid, status="no_html", detail="arXiv 未提供", actor="ai", reason="判据")
    none = reg.invoke("read_paper_text", arxiv_id=aid)
    assert none["ok"] is False and none["error"]["kind"] == "no_html"
    assert "不进精读体系" in none["error"]["hint"]


def test_marks_are_undoable(tmp_path):
    """批注是长期资产：写错能撤、删错能恢复（事件里存了完整快照）。"""
    c = _container(tmp_path)
    aid = _paper(c)
    _archive(c, aid)
    reg = registry_for(c)
    m = reg.invoke("annotate_paper", arxiv_id=aid, block="S2.p1", body="待撤销",
                   actor="ai", reason="判据")["mark"]
    reg.invoke("delete_mark", mark_id=m["id"], actor="human", reason="判据删")
    assert c.repo.marks_for(aid) == []
    assert reg.invoke("undo", seq=0, reason="判据撤销")["ok"] is True
    back = c.repo.marks_for(aid)
    assert len(back) == 1 and back[0]["body"] == "待撤销"


def test_delete_mark_is_human_only(tmp_path):
    """删除批注**不投影给 AI**（第一道锁）——工具面里没有它，AI 只能 undo 自己写的。"""
    from paperpilot.mecha_adapter.tools import TOOL_TO_CAPABILITY

    assert "delete_mark" not in set(TOOL_TO_CAPABILITY.values())
    c = _container(tmp_path)
    assert registry_for(c).get("delete_mark") is not None      # 能力在，只是不给 AI


# ---------------------------------------------------------------- 界面②：阅读页


def test_reading_page_serves_same_origin_html_and_marks(tmp_path):
    """阅读页 = 同源正文 + 批注列表 + 增量接口；人侧删除走能力层（无栈回退）。"""
    c = _container(tmp_path)
    aid = _paper(c)
    _archive(c, aid)
    reg = registry_for(c)
    mark = reg.invoke("annotate_paper", arxiv_id=aid, block="S2.p2", body="看图",
                      actor="ai", reason="判据")["mark"]
    client = TestClient(create_app(c, None), follow_redirects=True)

    page = client.get(f"/read/{aid}")
    assert page.status_code == 200
    assert "批注" in page.text and aid in page.text
    assert f"/paper/{aid}/html" in page.text                  # 正文走**同源**路由
    assert "sandbox" in page.text and "allow-scripts" not in page.text

    raw = client.get(f"/paper/{aid}/html")
    assert raw.status_code == 200 and "String breaking is observed" in raw.text
    assert "<script" not in raw.text                          # 外部内容已被消毒

    js = client.get(f"/read/{aid}/marks.json").json()
    assert [m["id"] for m in js["marks"]] == [mark["id"]]
    assert client.get(f"/read/{aid}/marks.json?since_id={mark['id']}").json()["count"] == 0

    assert client.post(f"/read/{aid}/marks/{mark['id']}/delete").status_code == 200
    assert c.repo.marks_for(aid) == []


def test_reading_page_tells_the_truth_when_there_is_no_html(tmp_path):
    """没归档/没 HTML 时页面**如实说明并指路**，绝不假装有正文。"""
    c = _container(tmp_path)
    aid = _paper(c)
    client = TestClient(create_app(c, None), follow_redirects=True)
    page = client.get(f"/read/{aid}")
    assert page.status_code == 200 and "还没有可用的 HTML 正文" in page.text
    assert client.get(f"/paper/{aid}/html").status_code == 404


def _container(tmp_path):
    from paperpilot.app.container import build_container

    c = build_container(make_settings(tmp_path / "data"))
    return c

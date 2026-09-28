"""按关注作者回填：把 arXiv 上查到的 6 篇入库存档（actor=ai 留痕），并打印摘要供写卡。"""


from paperpilot.app.container import build_container
from paperpilot.config import load_settings

settings = load_settings()
container = build_container(load_settings())

from paperpilot.infra.arxiv import ArxivClient  # noqa: E402

client = ArxivClient(cache_dir=settings.cache_dir / "arxiv")
ids = ["2608.23711", "2608.23694", "2608.10534", "2608.24981", "2609.11668", "2609.03039"]


def safe(s):
    return (s or "").encode("gbk", "replace").decode("gbk")


found = {}
try:
    for aid in ids:
        papers = client.collect(f'id_list={aid}', max_per_query=5)
        if papers:
            found[aid] = papers[0]
finally:
    client.close()

if found:
    res = container.repo.upsert_papers(
        list(found.values()), actor="ai",
        reason="按关注作者回填（arXiv au: 查询命中）：陈丞/翟慧/尤力/Lukin 8-9 月论文",
    )
    print(f"入库: 新增 {res['new']}，更新 {res['updated']}")

papers = {p.arxiv_id: p for p in container.repo.recent_papers(limit=10_000)}
for aid in ids:
    p = papers.get(aid)
    if p is None:
        print(f"\n### {aid} 未入库")
        continue
    print(f"\n### {aid} [{p.primary_category}] status={p.status}")
    print("T:", safe(p.title))
    print("AU:", safe(", ".join(p.authors or [])))
    print("AB:", safe((p.abstract or "")[:600]))

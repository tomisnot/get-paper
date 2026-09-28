"""时间窗口回放：按真实主题 query 拉取 2026-08-24 当天提交的 arXiv 论文入库
（与每日 fetch_papers 同款查询，只是窗口钉在 08-24 全天）。"""

from datetime import datetime

from paperpilot.app.container import build_container
from paperpilot.config import load_settings
from paperpilot.infra.arxiv import ArxivClient

settings = load_settings()
container = build_container(load_settings())
client = ArxivClient(cache_dir=settings.cache_dir / "arxiv")

since = datetime(2026, 8, 24, 0, 0)
until = datetime(2026, 8, 24, 23, 59)
try:
    papers = client.fetch_candidates(
        topics=settings.topics,
        categories=settings.arxiv_categories,
        since=since,
        until=until,
    )
finally:
    client.close()

print(f"08-24 当天命中主题查询: {len(papers)} 篇")
for p in papers:
    print(f"  {p.arxiv_id} [{p.primary_category}] {p.title[:66]}")

if papers:
    res = container.repo.upsert_papers(
        papers, actor="ai",
        reason="时间窗口回放测试：抓取 2026-08-24 当天提交的论文（做当日日报用）",
    )
    print(f"\n入库: 新增 {res['new']}，更新 {res['updated']}")

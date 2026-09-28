"""按作者裸查 arXiv（绕开本库的关键词门），确认四位学者+Lukin 近两月的论文。
用标准拉丁拼写；用于确认拼写形式与论文是否存在。"""

from datetime import datetime

from paperpilot.config import load_settings
from paperpilot.infra.arxiv import ArxivClient

settings = load_settings()
client = ArxivClient(cache_dir=settings.cache_dir / "arxiv")

since = datetime(2026, 7, 28)
until = datetime(2026, 9, 26, 23, 59)
window = f"submittedDate:[{since.strftime('%Y%m%d%H%M')} TO {until.strftime('%Y%m%d%H%M')}]"

authors = ["Cheng Chen", "Hui Zhai", "You Li", "Chaoyang Lu", "Mikhail Lukin"]
try:
    for au in authors:
        q = f'{window} AND au:"{au}"'
        papers = client.collect(q, max_per_query=30)
        print(f"\n==== au:{au} —— {len(papers)} 篇 ====")
        for p in papers:
            print(f"  {p.arxiv_id} [{p.primary_category}] {str(p.published_at)[:10]} {p.title[:64]}")
            print(f"      authors: {', '.join((p.authors or [])[:4])}")
finally:
    client.close()

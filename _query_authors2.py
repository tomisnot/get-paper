"""按作者查 arXiv（近两月）+ 只列物理类论文（避开同名碰撞）+ 全作者列表辨真身。"""

from datetime import datetime

from paperpilot.config import load_settings
from paperpilot.infra.arxiv import ArxivClient

settings = load_settings()
client = ArxivClient(cache_dir=settings.cache_dir / "arxiv")

since = datetime(2026, 7, 28)
until = datetime(2026, 9, 26, 23, 59)
window = f"submittedDate:[{since.strftime('%Y%m%d%H%M')} TO {until.strftime('%Y%m%d%H%M')}]"

PHYS = ("quant-ph", "cond-mat", "physics.")


def safe(s):
    return (s or "").encode("gbk", "replace").decode("gbk")


authors = ["Cheng Chen", "Chen Cheng", "Hui Zhai", "You Li", "Chaoyang Lu", "Mikhail Lukin"]
try:
    for au in authors:
        try:
            papers = client.collect(f'{window} AND au:"{au}"', max_per_query=40)
        except Exception as exc:  # noqa: BLE001
            print(f"\n==== au:{au} —— 查询失败: {exc}")
            continue
        phys = [p for p in papers if (p.primary_category or "").startswith(PHYS)]
        print(f"\n==== au:{au} —— 命中 {len(papers)} 篇，其中物理类 {len(phys)} 篇 ====")
        for p in phys[:12]:
            print(f"  {p.arxiv_id} [{p.primary_category}] {str(p.published_at)[:10]} {safe(p.title)[:62]}")
            print(f"      {safe(', '.join(p.authors or [])[:110])}")
finally:
    client.close()

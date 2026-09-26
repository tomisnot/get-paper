"""简报渲染：BriefingContent → Markdown（Web 页面用同源数据渲染，这里只负责文件/通知格式）。"""

from __future__ import annotations

from collections import defaultdict

from ..domain.models import BriefingContent, BriefingItem

_DETAIL_LABELS = (
    ("problem", "问题"),
    ("method", "方法"),
    ("results", "结论"),
    ("novelty", "贡献点"),
)


class MarkdownBriefRenderer:
    """DESIGN.md §7.1 的简报版式。"""

    def render(self, content: BriefingContent) -> str:
        stats = content.stats
        lines: list[str] = [f"# arXiv 每日简报 · {content.date}", ""]

        mode = "AI 精选 + AI 精读" if stats.ai_enabled else "规则筛选（AI 关闭）"
        lines.append(
            f"> 今日候选 **{stats.fetched}** 篇 → 过规则 **{stats.after_rules}** 篇 → "
            f"精选 **{stats.selected}** 篇（{mode} · provider=`{stats.ai_provider}` · "
            f"AI 耗时 {stats.ai_latency_ms}ms）"
        )
        for msg in stats.degraded:
            lines.append(">")
            lines.append(f"> ⚠️ {msg}")
        lines.append("")

        must = [i for i in content.selected if i.label == "must_read"]
        worth = [i for i in content.selected if i.label != "must_read"]
        for section, items in (("今日必读", must), ("值得一看", worth)):
            if not items:
                continue
            lines.append(f"## {section}")
            lines.append("")
            lines.extend(self._render_items(items, detailed=(section == "今日必读")))

        by_cat: dict[str, list[BriefingItem]] = defaultdict(list)
        for item in content.selected:
            by_cat[item.primary_category or "其他"].append(item)
        if by_cat:
            lines.append("## 分类速览")
            lines.append("")
            lines.append("| 分类 | 篇数 | 精选 | 代表论文 |")
            lines.append("| --- | --- | --- | --- |")
            for cat, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
                top = max(items, key=lambda i: i.score)
                lines.append(
                    f"| {cat} | {len(items)} | {len(items)} | "
                    f"[{_short(top.title, 40)}]({top.abs_url})（{top.score:.2f}） |"
                )
            lines.append("")

        if content.archived:
            lines.append("<details><summary>存档（过规则未入选，可翻案）</summary>")
            lines.append("")
            for item in content.archived[:40]:
                lines.append(
                    f"- [{_short(item.title, 60)}](https://arxiv.org/abs/{item.arxiv_id}) "
                    f"— {item.score:.2f} — {item.primary_category}"
                )
            if len(content.archived) > 40:
                lines.append(f"- ……其余 {len(content.archived) - 40} 篇见论文库")
            lines.append("")
            lines.append("</details>")
            lines.append("")

        lines.append("---")
        lines.append(f"*由 PaperPilot 自动生成 · {content.date}*")
        return "\n".join(lines) + "\n"

    def _render_items(self, items: list[BriefingItem], *, detailed: bool) -> list[str]:
        lines: list[str] = []
        for idx, item in enumerate(items, start=1):
            lines.append(f"### {idx}. {item.title}")
            lines.append("")
            meta_bits = [f"相关性 **{item.score:.2f}**"]
            if item.primary_category:
                meta_bits.append(item.primary_category)
            if item.published_at:
                meta_bits.append(item.published_at.strftime("%Y-%m-%d"))
            authors = ", ".join(item.authors[:3]) + (" 等" if len(item.authors) > 3 else "")
            lines.append(f"{authors} · {' · '.join(meta_bits)}")
            lines.append("")

            summary = item.summary
            if summary is not None:
                if summary.tldr:
                    lines.append(f"- **TL;DR**：{summary.tldr}")
                for key, label in _DETAIL_LABELS:
                    value = getattr(summary, key, "")
                    if value:
                        lines.append(f"- **{label}**：{value}")
            if not item.ai_summary:
                lines.append("- *（AI 不可用，以上为原文摘编）*")
            if item.reason:
                lines.append(f"- **推荐理由**：{item.reason}")
            if item.tags:
                lines.append(f"- **命中**：{', '.join(item.tags)}")
            if not detailed:
                lines.append(f"- [原文]({item.abs_url}) · [PDF]({item.pdf_url})")
            lines.append("")
        return lines


def _short(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"

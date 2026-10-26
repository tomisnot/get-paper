"""arXiv 编号工具：一篇论文只有一个 id，版本号单独存。"""

from __future__ import annotations

import re

_VERSION_RE = re.compile(r"^(?P<base>.+?)(?P<ver>v\d+)?$")


def split_id(raw: str) -> tuple[str, str]:
    """`2601.12345v3` → (`2601.12345`, `3`)。"""
    text = (raw or "").strip()
    if not text:
        return "", ""
    text = text.split("/abs/")[-1]
    text = text.split("/pdf/")[-1]
    if text.endswith(".pdf"):
        text = text[:-4]
    m = _VERSION_RE.match(text)
    if not m:
        return text, ""
    base = m.group("base")
    ver = (m.group("ver") or "").lstrip("v")
    return base, ver


def base_id(raw: str) -> str:
    return split_id(raw)[0]


def abs_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/abs/{arxiv_id}"


def pdf_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/pdf/{arxiv_id}"

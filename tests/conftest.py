from __future__ import annotations

import pytest

from paperpilot.config import Settings
from paperpilot.context import build_context
from paperpilot.mecha_adapter.stack import build_stack


@pytest.fixture
def ctx(tmp_path):
    settings = Settings(data_dir=str(tmp_path))
    context = build_context(settings=settings)
    yield context
    context.close()


@pytest.fixture
def stack(ctx, tmp_path):
    value = build_stack(ctx, root=tmp_path / "root")
    yield value
    value.close()


def seed_paper(repo, arxiv_id: str, *, title: str = "", categories=None, authors=None,
               published: str = "2026-10-01 00:00:00", found_by: str = "test") -> str:
    categories = categories or ["quant-ph"]
    repo.upsert_papers([{
        "arxiv_id": arxiv_id,
        "title": title or f"Paper {arxiv_id}",
        "abstract": "abstract text",
        "authors": authors or ["Alice"],
        "categories": categories,
        "primary_category": categories[0],
        "published_at": published,
        "updated_at": published,
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
    }], found_by=found_by)
    return arxiv_id


@pytest.fixture
def paper(ctx):
    def _make(arxiv_id: str = "2610.00001", **kwargs):
        return seed_paper(ctx.repo, arxiv_id, **kwargs)
    return _make

"""pytest 公共 fixtures。"""

from __future__ import annotations

from pathlib import Path

import pytest

from paperpilot.config import AICfg, ScoringCfg, Settings, TopicCfg
from paperpilot.domain.pipeline import DailyPipelineService
from paperpilot.infra.ai import HeuristicRanker, HeuristicSummarizer
from paperpilot.infra.arxiv import parse_atom
from paperpilot.infra.db import init_db, make_engine, make_session_factory
from paperpilot.infra.fts import PaperIndex
from paperpilot.infra.render import MarkdownBriefRenderer
from paperpilot.infra.repo import PaperRepository

SAMPLE_XML = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "paperpilot"
    / "data"
    / "sample_arxiv.xml"
)


def make_settings(data_dir: Path) -> Settings:
    settings = Settings(
        data_dir=data_dir,
        lookback_days=30,
        topics=[
            TopicCfg(
                name="推理与 Test-Time Compute",
                categories=["cs.CL", "cs.AI"],
                keywords=[
                    "reasoning",
                    "chain-of-thought",
                    "test-time compute",
                    "process reward",
                    "inference-time scaling",
                    "self-consistency",
                ],
                exclude_keywords=["survey"],
                quota=2,
                threshold=0.5,
            ),
            TopicCfg(
                name="RAG 与长上下文",
                categories=["cs.CL", "cs.IR"],
                keywords=[
                    "retrieval-augmented",
                    "dense retrieval",
                    "knowledge base",
                    "long context",
                ],
                exclude_keywords=["survey"],
                quota=2,
                threshold=0.5,
            ),
            TopicCfg(
                name="高效微调与推理",
                categories=["cs.LG", "cs.CL"],
                keywords=[
                    "LoRA",
                    "quantization",
                    "KV cache",
                    "efficient inference",
                    "speculative decoding",
                ],
                exclude_keywords=["workshop"],
                quota=2,
                threshold=0.5,
            ),
        ],
        scoring=ScoringCfg(
            threshold=0.5, quota_per_topic=2, max_papers=8, max_per_author=1, must_read_cap=3
        ),
        ai=AICfg(provider="heuristic"),
    )
    # 测试环境：配置写回临时目录，不碰真实 config/settings.yaml
    settings.config_path = data_dir.parent / "settings.yaml"
    return settings


@pytest.fixture(autouse=True)
def _no_llm_keys(monkeypatch):
    """测试环境确定性：屏蔽真实 API key 环境变量（避免容器档位随环境漂移）。"""
    monkeypatch.delenv("PAPERPILOT_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


@pytest.fixture
def sample_papers():
    """内置样例 Atom 解析结果（不联网）。"""
    return parse_atom(SAMPLE_XML.read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path / "data")


@pytest.fixture
def repo(tmp_path):
    engine = make_engine(tmp_path / "test.sqlite3")
    init_db(engine)
    index = PaperIndex(engine)
    return PaperRepository(make_session_factory(engine), index=index)


@pytest.fixture
def pipeline(settings, repo) -> DailyPipelineService:
    return DailyPipelineService(
        repo,
        settings=settings,
        ranker=HeuristicRanker(),
        summarizer=HeuristicSummarizer(),
        renderer=MarkdownBriefRenderer(),
        ai_provider="heuristic",
    )


@pytest.fixture
def loaded_repo(repo, sample_papers):
    repo.sync_topics(make_settings(_repo_db_dir(repo)).topics)
    repo.upsert_papers(sample_papers)
    return repo


def _repo_db_dir(repo) -> Path:
    """从 repo 反推 sqlite 所在目录（仅测试辅助）。"""
    url = str(repo.sf.kw["bind"].url)
    return Path(url.replace("sqlite:///", "")).parent


@pytest.fixture
def container(settings):
    """完整 DI 容器（heuristic 档、tmp 数据目录）——能力层/CLI/Web 测试用。"""
    from paperpilot.app.container import build_container

    return build_container(settings)

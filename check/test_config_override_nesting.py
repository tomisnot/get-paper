"""配置覆盖的**嵌套语义**判据（BUG-2026-10-06）。

事故：`finalize_briefing(max_items=N)` 从 **YAML settings** 重建了一层配置覆盖，
把命令面刚注入的 Gate 快照（`set_config` 写的 `max_per_author` 等）**整体打回默认值**，
还顺手清掉了 `lookback_days`。而 `read_config` 读的是 Gate 快照、显示正常
⇒ **错误只在选择结果里显形**（用户可见产物静默错误）。

两条不变式（下面三个判据分别钉住）：
1. **合并而非替换**：`pipeline_config_override` 里 `None = 不动这个字段`（不是"清空"）；
2. **显式参数只赢它自己那一个键**：`max_items` 只该改 `max_papers`，其余继承**当前生效值**；
3. **结构性守卫**：能力层不许再从 `settings.scoring` 重建覆盖（那是同族 bug 的温床）。
"""

from __future__ import annotations

from pathlib import Path

from paperpilot.config import ScoringCfg
from paperpilot.domain.pipeline import DailyPipelineService, pipeline_config_override
from tests.test_mecha_adapter import _call, _stack


def test_nested_override_merges_instead_of_clearing(tmp_path):
    """①合并语义：内层只叠 `scoring` 时，**不许**把外层的 `lookback_days` 清掉。

    旧实现是无条件赋值（`None` 也赋）⇒ "None = 不覆盖"的 docstring 与实现不符，
    嵌套时内层就把外层清空了。这里直接照嵌套场景断言。
    """
    c, _ = _stack(tmp_path)
    outer = ScoringCfg(max_papers=9, max_per_author=3, must_read_cap=2)
    with pipeline_config_override(scoring=outer, lookback_days=7):
        assert c.pipeline.effective_lookback_days() == 7
        inner = outer.model_copy(update={"max_papers": 5})
        with pipeline_config_override(scoring=inner):          # 故意不传 lookback
            assert c.pipeline.effective_lookback_days() == 7, "内层把外层 lookback 清掉了"
            assert c.pipeline.effective_scoring().max_per_author == 3
        # 内层退出 ⇒ 外层完好
        assert c.pipeline.effective_scoring().max_papers == 9
        assert c.pipeline.effective_scoring().must_read_cap == 2
    # 全部退出 ⇒ 回落 settings
    assert c.pipeline.effective_lookback_days() == c.settings.lookback_days
    assert c.pipeline.effective_scoring().max_per_author == c.settings.scoring.max_per_author


def test_finalize_max_items_only_wins_its_own_key(tmp_path, monkeypatch):
    """②本 bug：`max_items` 只赢 `max_papers`；`set_config` 写的其余键必须继承。

    走**真实的命令面**写 Gate 快照（与实测复现同路径），再以 `max_items` 定稿，
    并在调用期间**抓取生效配置**——断言的是机制本身，不依赖池子里恰好有同作者论文。
    """
    c, stack = _stack(tmp_path)
    tools = stack["tools"]

    assert _call(tools, "set_config", key="scoring.max_per_author", value=3)["ok"]
    assert _call(tools, "set_config", key="scoring.must_read_cap", value=2)["ok"]
    assert _call(tools, "set_config", key="scoring.max_papers", value=9)["ok"]

    prepared = _call(tools, "prepare_review")
    assert prepared["ok"] and prepared["candidates"], prepared
    date = prepared["date"]
    reviews = [{"arxiv_id": x["arxiv_id"], "score": 0.8, "label": "worth", "reason": "判据"}
               for x in prepared["candidates"]]
    assert _call(tools, "submit_review", date=date, reviews=reviews)["ok"]

    seen = []
    real = DailyPipelineService.effective_scoring

    def spy(self):
        value = real(self)
        seen.append(value)
        return value

    monkeypatch.setattr(DailyPipelineService, "effective_scoring", spy)
    out = _call(tools, "finalize_briefing", date=date, force=True, max_items=5)
    assert out["ok"], out
    assert out["max_items_applied"] == 5
    assert seen, "定稿期间没读到生效配置 ⇒ 判据没盖到目标路径（改坏了要红）"

    inside = seen[-1]
    assert inside.max_papers == 5, "显式 `max_items` 没赢下 `max_papers`"
    assert inside.max_per_author == 3, (
        f"`set_config` 的 max_per_author 被遮蔽了（实得 {inside.max_per_author}，"
        "BUG-2026-10-06 复现：内层从 YAML 重建，掉回默认 1）")
    assert inside.must_read_cap == 2, "其余键也没继承（同一个根因）"


def test_no_capability_rebuilds_config_from_settings():
    """③结构性守卫（防同族复发）：能力层不许再用 `settings.scoring.….model_copy(...)` 造覆盖。

    那是"从 YAML 重建"，只要嵌在命令面注入之内就会遮蔽其余键——本 bug 的成因。
    正确姿势是从 **生效值** 派生：`pipeline.effective_scoring().model_copy(update={…})`。

    ⚠ 走 **AST** 而不是源码文本：文本匹配会把"解释这个 bug 的注释"也算进去
    （第一版就是这么红的——注释里正写着那个错误写法）。AST 只认真实代码里的属性链。
    """
    import ast

    path = (Path(__file__).resolve().parents[1] / "src" / "paperpilot"
            / "capabilities" / "tools.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == "model_copy"):
            continue
        chain, cur = [], node.value
        while isinstance(cur, ast.Attribute):
            chain.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            chain.append(cur.id)
        root = chain[::-1]                      # 例：["settings", "scoring"]
        if root[:2] == ["settings", "scoring"]:
            bad.append(ast.unparse(node))
    assert not bad, (
        f"又有人在能力层从 YAML 重建配置覆盖了（BUG-2026-10-06 同族雷区）：{bad}")

    src = path.read_text(encoding="utf-8")
    assert "effective_scoring()" in src, "`max_items` 那条应从生效配置派生"

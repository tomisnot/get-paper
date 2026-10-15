"""PaperPilot 的本地流水线协调器。

领域只出现在这里：流水线执行和资源估算由 GP 自己决定；mecha 只接收其命令声明。

配置态（评分阈值 / lookback 等标量）声明在 ``CONFIG_SCHEMA``，由
``make_validator`` 做类型/值域校验。
"""

from __future__ import annotations

from mecha.errors import GateDenied, UnknownKey
from ..domain.pipeline import pipeline_config_override

#: 可写标量配置键的类型/值域（Phase 2 迁入 Gate；单一来源在此声明）。
#: 变长配置（主题列表）不在此列——它走命令，不走 flat-KV 门（交接文档 §7 Phase 2）。
#: tools.py 的 set_config/read_config 也引用它（配置键的单一来源）。
CONFIG_SCHEMA: dict[str, dict] = {
    "lookback_days": {"type": "int", "lo": 1, "hi": 365,
                      "desc": "抓取回溯天数"},
    "scoring.threshold": {"type": "float", "lo": 0.0, "hi": 1.0,
                          "desc": "入选评分阈值（0-1）"},
    "scoring.quota_per_topic": {"type": "int", "lo": 1, "hi": 50,
                                "desc": "每主题入选配额"},
    "scoring.max_papers": {"type": "int", "lo": 1, "hi": 200,
                           "desc": "单期简报最多入选篇数"},
    "scoring.max_per_author": {"type": "int", "lo": 1, "hi": 50,
                               "desc": "同一作者最多入选篇数"},
    "scoring.must_read_cap": {"type": "int", "lo": 0, "hi": 50,
                              "desc": "必读档位上限"},
}


def gate_scoring_override(container, gate):
    """从 gate 快照造本次调用的配置覆盖（ScoringCfg 副本 + lookback）；无 gate 则 (None, None)。

    ⭐ N15：这是**命令面全路径**的配置权威接缝——run_pipeline、
    prepare/submit/finalize 各段都吃到 set_config 的值，
    否则 set_config 对 finalize 成静默 no-op（silent no-op 比响亮拒绝更坏）。
    **不改共享 settings**：返回副本，交 ``pipeline_config_override`` 做线程局部覆盖。
    """
    if gate is None:
        return None, None
    snap = gate.snapshot
    settings = container.settings
    lookback = snap.get("lookback_days")
    updates: dict[str, object] = {}
    for key, attr in (("scoring.threshold", "threshold"),
                      ("scoring.quota_per_topic", "quota_per_topic"),
                      ("scoring.max_papers", "max_papers"),
                      ("scoring.max_per_author", "max_per_author"),
                      ("scoring.must_read_cap", "must_read_cap")):
        if key in snap and snap[key] is not None:
            updates[attr] = snap[key]
    scoring = settings.scoring.model_copy(update=updates) if updates else None
    return scoring, lookback



def make_validator():
    """Gate 的 validate：标量配置键的类型/值域校验，报错即教学（L4）。

    Phase 1 只在装配期挂上（尚未 seed/set）；Phase 2 把 settings.yaml 的标量迁入 Gate 时即用它守门。
    ⚠ **不再需要给 `command.` 前缀开白名单**（2026-09-26 起）：框架改成 `Gate.record`
    **只记史、不改快照** ⇒ 命令审计键**永远不会经 `Gate.set` 走到这里**。原先那句
    `if key.startswith("command."): return` 因此成了**不可达分支**（R17）⇒ **已删**。
    兜住它的判据：`tests/test_mecha_cockpit.py` 断言"域快照里没有 `command.*`、`n_keys == 6`"
    ——框架若回退（审计键又掺进快照）⇒ 那条立刻红（那时才需要把白名单加回来）。
    """
    schema = dict(CONFIG_SCHEMA)  # type: ignore[arg-type]
    settable = set(schema)

    def validate(key: str, value: object) -> None:
        if key not in settable:
            raise UnknownKey.typo(
                key, settable,
                kind="unknown_key",
                hint=f"可写标量配置键共 {len(settable)} 个；变长配置（主题）走命令不走访",
            )
        if value is None:            # None = 清空回默认（与 GUI「留空=默认」同义）
            return
        rec = schema[key]
        tname = rec.get("type")
        if tname == "int" and (isinstance(value, bool) or not isinstance(value, int)):
            raise GateDenied(f"{key} 须是整数，收到 {type(value).__name__}",
                             kind="bad_value", hint=f"{key}：{rec.get('desc')}")
        if tname == "float" and (isinstance(value, bool)
                                 or not isinstance(value, (int, float))):
            raise GateDenied(f"{key} 须是数值，收到 {type(value).__name__}",
                             kind="bad_value", hint=f"{key}：{rec.get('desc')}")
        lo, hi = rec.get("lo"), rec.get("hi")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if lo is not None and value < lo:
                raise GateDenied(f"{key}={value} 低于下限 {lo}", kind="bad_value",
                                 hint=f"{key} 合法区间 [{lo}, {hi}]")
            if hi is not None and value > hi:
                raise GateDenied(f"{key}={value} 高于上限 {hi}", kind="bad_value",
                                 hint=f"{key} 合法区间 [{lo}, {hi}]")

    return validate


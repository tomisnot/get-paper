"""PaperPilotEngine：把 ``container.pipeline`` 包装成 mecha 的 4 必实现。

领域只出现在这里（接入指南第 1 步）：``run`` = 每日流水线一键跑；``schema``
声明回执必填键与可写标量配置；``estimate`` 给受 arXiv 限速主导的粗估。

配置态（评分阈值 / lookback 等标量）声明在 ``schema()['config']``，由
``make_validator`` 做类型/值域校验——Phase 2 把它们迁进 Gate 时，校验口径不变。
"""

from __future__ import annotations

from collections.abc import Mapping

from mecha.errors import GateDenied, UnknownKey
from mecha.surface import Engine, ExecutionContext

from .. import capabilities

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


class PaperPilotEngine(Engine):
    """4 必实现：每日流水线的运行面（领域实现仍住 capabilities/pipeline）。"""

    def __init__(self, container, gate=None) -> None:
        self._container = container
        self._gate = gate          # 装配后由 hub 回填（engine 先于 gate 建成）

    def attach_gate(self, gate) -> None:
        """回填 Gate：让 ``run`` 把 gate 快照里的标量配置叠加到本次运行。

        engine 是 ``assemble()`` 的入参、gate 是 ``assemble()`` 的产物 ⇒ 二者
        构造有先后，故用回填而非构造注入（避免循环）。
        """
        self._gate = gate

    # ---- 必实现 ①：一处声明的 schema ----

    def schema(self) -> Mapping[str, object]:
        return {
            "run_output_required": ["ok", "date", "selected"],
            "estimate_output_required": ["est_sec"],
            "config": dict(CONFIG_SCHEMA),
            "settable_keys": sorted(CONFIG_SCHEMA),
        }

    # ---- 必实现 ②：run = 一键每日流水线 ----

    def run(self, spec: Mapping[str, object], *, context: ExecutionContext,
            **opts: object) -> Mapping[str, object]:
        # 长活（抓取受 arXiv 限速）：尽力而为地把取消透传给流水线目前无接缝，
        # 这里只在跑前尊重取消请求（同进程无法强杀正在进行的网络调用）。
        if context.cancel_event.is_set():
            return {"ok": False, "date": str(spec.get("date") or ""),
                    "selected": 0, "error": {"kind": "cancelled",
                                             "message": "运行前已请求取消"}}
        context.report_progress(0.1, "pipeline start")
        # 配置态归 Gate（Scheme E）：把 gate 快照里的标量叠加到本次运行，让
        # set_config 真正对流水线生效（否则 gate 配置只是装饰 = 假绿）。叠加是
        # 临时的（跑完还原 settings）——gate 是 AI 运行路径的配置权威，settings.yaml
        # 是人类路径（Web/CLI）与启动种子的来源；二者分叉记进 n=3 报告。
        restore = self._apply_gate_config()
        try:
            res = capabilities.invoke(
                self._container, "run_pipeline",
                date=str(spec.get("date") or ""),
                force=bool(spec.get("force", False)),
                actor=str(spec.get("actor") or "ai"),
                reason=str(spec.get("reason") or ""),
            )
        finally:
            restore()
        context.report_progress(1.0, "pipeline done")
        # 回执必须含 run_output_required 键（失败信封也要补齐，surface.run 会校验）。
        out = dict(res)
        out.setdefault("date", str(spec.get("date") or ""))
        out.setdefault("selected", 0)
        return out

    # ---- 配置叠加（run 的内部助手）----

    def _apply_gate_config(self):
        """把 gate 快照里的标量配置叠加到 settings，返回还原回调（无 gate 则空操作）。"""
        if self._gate is None:
            return lambda: None
        snap = self._gate.snapshot
        s = self._container.settings
        targets = (("lookback_days", s, "lookback_days"),
                   ("scoring.threshold", s.scoring, "threshold"),
                   ("scoring.quota_per_topic", s.scoring, "quota_per_topic"),
                   ("scoring.max_papers", s.scoring, "max_papers"),
                   ("scoring.max_per_author", s.scoring, "max_per_author"),
                   ("scoring.must_read_cap", s.scoring, "must_read_cap"))
        saved: list[tuple] = []
        for key, obj, attr in targets:
            if key in snap and snap[key] is not None:
                saved.append((obj, attr, getattr(obj, attr)))
                setattr(obj, attr, snap[key])

        def restore() -> None:
            for obj, attr, old in reversed(saved):
                setattr(obj, attr, old)
        return restore

    # ---- 必实现 ③：summary ----

    def summary(self) -> Mapping[str, object]:
        settings = self._container.settings
        return {
            "engine": "paperpilot",
            "ai_provider": self._container.ai_provider,
            "topics": len(settings.topics),
            "lookback_days": settings.lookback_days,
        }

    # ---- 必实现 ④：estimate（补框架必填 est_sec，粗估口径写进 est_note）----

    def estimate(self, spec: Mapping[str, object]) -> Mapping[str, object]:
        settings = self._container.settings
        days = int(spec.get("lookback_days") or settings.lookback_days or 1)
        # 粗估：抓取受 arXiv 3s 限速主导，按主题数与回溯天数线性外推。
        est = round(5.0 + 3.0 * len(settings.topics) * max(1, days) / 3.0, 1)
        return {"est_sec": est,
                "est_note": "粗估：抓取受 arXiv 限速主导；打分/渲染按启发式档计"}


def make_validator(engine: PaperPilotEngine):
    """Gate 的 validate：标量配置键的类型/值域校验，报错即教学（L4）。

    Phase 1 只在装配期挂上（尚未 seed/set）；Phase 2 把 settings.yaml 的标量
    迁入 Gate 时即用它守门。命令审计键（``command.`` 前缀）留到 Phase 2 接线。
    """
    schema = dict(engine.schema()["config"])  # type: ignore[arg-type]
    settable = set(schema)

    def validate(key: str, value: object) -> None:
        # 命令审计键（command.<name>）由框架写进 History（commands._audit）：
        # 值是审计记录不是配置标量，validator 必须放行该前缀（否则 command_audit_denied）。
        if key.startswith("command."):
            return
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

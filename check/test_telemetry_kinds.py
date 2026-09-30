"""能力 `kind` 与"它到底写没写痕"的**对账判据**（项目侧）。

## 为什么这条只能在项目侧（框架侧已定案）
框架 ADR `mecha/docs/v2/notes/implemented/2026-09-26-读写遥测三档不进框架.md`：**框架看不到你的工具体**
⇒ 即使加一个声明位，也**管不住"它偷偷写了"**，那个声明位**唯一的消费者就是项目侧判据** ⇒ 不进框架。
《接入指南》第三条给了可照抄的形状（本文按它写），并明确：**第 3 步"双向"不能省**（否则判据可能永远绿）。

## 判定口径（**只声明已验证的部分**）
* `telemetry` ⟺ op 以 **`telemetry.`** 开头
* `signal`    ⟺ op 以 **`signal:`** 开头（兴趣信号，`repo.py` 的 `record_signal`）
⇒ 本文件只断言【两者**互不计入**】。
⚠ **如实注明已知粗粒度**：**"其余"这个桶目前既含业务事件、也含启动/配置痕**
（`migrate` / `sync_topics_boot` / `set_settings` —— 它们是"启动痕/配置痕"，**不是能力遥测**）。
⇒ **本文件不写"其余即业务"**：那是**没验过的更强断言**。将来若需要第三档（如 `system.*`）再单开一批。
"""

from __future__ import annotations

import pytest
from mecha.authority import Mode

from paperpilot import capabilities
from paperpilot.app.container import build_container
from paperpilot.mecha_adapter.tools import TOOL_DECLS

from .conftest import make_settings

_SIGNAL_PREFIX = "signal:"
_TELEMETRY_PREFIX = "telemetry."


def _reg(tmp_path):
    """建 container + 栈 + **灌样例语料**（否则 `star_paper` 会 not_found、`feed_generate` 会早退
    ⇒ 判据测的就不是"它写不写痕"，而是"池子空不空"——我第一版就踩了这个假红）。"""
    from paperpilot.infra.arxiv import parse_atom

    from .conftest import SAMPLE_XML

    container = build_container(make_settings(tmp_path / "data"))
    container.repo.upsert_papers(parse_atom(SAMPLE_XML.read_text(encoding="utf-8")))
    from paperpilot.mecha_adapter.hub import build_stack

    build_stack(container, tmp_path, "mecha", mode=Mode.OPEN)
    return container, capabilities.registry_for(container)


def _ops(events) -> list[str]:
    """事件 op 名（域事件是 ORM 对象：属性访问；兼容 dict 形态）。"""
    return [str(getattr(e, "op", None) or (e["op"] if isinstance(e, dict) else "")) for e in events]


def _new_events(container, before_seq: int):
    return container.repo.events_since(since_seq=before_seq, limit=1000)["events"]


def _call_and_collect(reg, container, name: str, **args):
    """调一次能力，收集**这次调用新产生的**事件（按 seq 切）。"""
    before = container.repo.events_since(since_seq=0, limit=1000)["last_seq"]
    out = reg.invoke(name, **args)
    return out, _new_events(container, before)


def assert_kind_matches_behavior(reg, container, name: str, kind: str, **args) -> None:
    """按**声明的档**断言它留下的痕（《接入指南》第 2 步的形状）。

    * `read`           ⇒ **0 条**新事件；
    * `read_telemetry` ⇒ **≥1 条 `telemetry.*`**，且 **0 条 `signal:*`**（互不计入）；
    * `write`          ⇒ **产生业务事件**（这里用"不是 telemetry/signal 前缀"的事件来判），
      并与**可撤销槽位**（`reversible`）对齐。
    """
    out, new = _call_and_collect(reg, container, name, **args)
    assert out.get("ok") is True, f"{name} 调用失败：{out}"
    ops = _ops(new)
    telemetry = [o for o in ops if o.startswith(_TELEMETRY_PREFIX)]
    signals = [o for o in ops if o.startswith(_SIGNAL_PREFIX)]
    if kind == "read":
        assert not new, f"声明 read 的能力却留下了事件：{ops}"
    elif kind == "read_telemetry":
        assert telemetry, f"声明 read_telemetry 的能力没留下 telemetry.* 痕：{ops}"
        assert not signals, f"遥测不该被记成兴趣信号（前缀分离）：{ops}"
    elif kind == "write":
        business = [o for o in ops if not o.startswith((_TELEMETRY_PREFIX, _SIGNAL_PREFIX))]
        assert business, f"声明 write 的能力没产生业务事件：{ops}"
        # 与"可撤销槽位"对齐：write 能力要么可撤销（有 before 快照）、要么明确不可逆 ⇒ 这里只钉
        # "至少有一条事件，且它的 op 是业务 op"（更强的"必须可撤销"由各自的域判据负责）
    else:                                     # pragma: no cover - 兜底：未知档不静默放过
        raise AssertionError(f"未知 kind={kind!r}（本文件只认 read/read_telemetry/write）")


def test_read_capability_leaves_no_event(tmp_path):
    """`kind == read` ⇒ 这次调用**产生 0 条新事件**。"""
    container, reg = _reg(tmp_path)
    kinds = {s["name"]: s["kind"] for s in reg.specs()}
    assert kinds["list_topics"] == "read", "list_topics 应声明 read（本判据的口径前提）"
    assert_kind_matches_behavior(reg, container, "list_topics", "read")


def test_read_telemetry_declares_and_writes_only_telemetry(tmp_path):
    """⭐ `feed_generate`：**声明与行为对齐** —— 声明 `read_telemetry`，行为只留 `telemetry.*`。

    ⚠ 两个断言分得很清：**声明侧**（注册器里的 kind 就是 `read_telemetry`）与
    **行为侧**（它留下的痕是 `telemetry.` 前缀、且不被记成 `signal:*`）。
    """
    container, reg = _reg(tmp_path)
    kind = {s["name"]: s["kind"] for s in reg.specs()}["feed_generate"]
    assert kind == "read_telemetry", (
        f"feed_generate 的声明应与行为对齐为 read_telemetry（现在 {kind!r}）")
    assert_kind_matches_behavior(reg, container, "feed_generate", "read_telemetry",
                                 limit=5, days=120, seen_days=0)


def test_write_capability_writes_business_event(tmp_path):
    """`kind == write` ⇒ **产生业务事件**（且与可撤销槽位对齐：`star_paper` 是可逆的）。"""
    container, reg = _reg(tmp_path)
    kinds = {s["name"]: s["kind"] for s in reg.specs()}
    assert kinds["star_paper"] == "write"
    assert_kind_matches_behavior(reg, container, "star_paper", "write",
                                 arxiv_id="2608.01101", actor="ai")
    ev = container.repo.events_since(since_seq=0, op="star_paper")["events"][-1]
    # ⚠ `events_since` 回的是 **dict**（不是 ORM 对象）⇒ 用 key 取；我第一版用 getattr 默认 0，
    #    于是"可逆槽位"永远读成 0 ⇒ 假红（判据自己的 bug，不是产品的）。
    rev = ev["undoable"] if isinstance(ev, dict) else getattr(ev, "undoable", 0)
    assert int(rev) == 1, f"可撤销的 write 应落在 reversible 槽位上：{ev}"


def test_swapping_the_declared_kind_reddens(tmp_path):
    """⭐ **双向**（《接入指南》第 3 步，不能省）：把声明换一档 ⇒ **判据必须红**。

    证明这套判定**不是哑的**：`feed_generate` 声明 `read_telemetry` 时过；**假装它是 `read`** 时
    必须报"声明 read 却留下了事件"。
    """
    container, reg = _reg(tmp_path)
    assert_kind_matches_behavior(reg, container, "feed_generate", "read_telemetry",
                                 limit=3, days=120, seen_days=0)
    with pytest.raises(AssertionError, match="声明 read 的能力却留下了事件"):
        assert_kind_matches_behavior(reg, container, "feed_generate", "read",
                                     limit=3, days=120, seen_days=0)


def test_projection_kind_agrees_with_capability_kind(tmp_path):
    """**两个声明面不许漂移**：`ToolDecl.kind == "write"` ⇔ 能力 `kind == "write"`。

    投影侧只有"写/非写"两分（写 ⇒ 走命令桥、有 `command.*` 审计；其余 ⇒ 读桥、不过门）
    ⇒ `read_telemetry` 在投影侧**按读处理**。这条防的是"能力层改成遥测、投影侧忘了跟着想"。
    """
    _container, reg = _reg(tmp_path)
    cap_kind = {s["name"]: s["kind"] for s in reg.specs()}
    for decl in TOOL_DECLS:
        if decl.cap_name not in cap_kind:
            continue
        cap_is_write = cap_kind[decl.cap_name] == "write"
        decl_is_write = decl.kind == "write"
        assert cap_is_write == decl_is_write, (
            f"{decl.mecha_name}: 能力层 kind={cap_kind[decl.cap_name]!r} 与投影侧 kind={decl.kind!r} 漂移")

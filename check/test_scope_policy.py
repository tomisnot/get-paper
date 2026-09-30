"""`scope` 声明的**插头**判据：绑没绑、绑得对不对、将来忘配会不会红。

背景（框架侧问出来的活病灶）：本仓 22 条命令**全部**声明了非空 `scope`，但
`CommandRegistry.bind_scope_policy(...)` **从来没被调用过** ⇒ `invoke` 里
`if self._scope_policy is not None and spec.scope:`（`mecha/commands.py:432`）**整段跳过**
⇒ **声明在、消费者在，插头没插**：声明了 scope 的命令**任何通道都能调**（实测 `add_topic`
经 AI 工具面**静默成功**）。框架那行代码**完全合法**（字段有消费者、框架自测也有判据）
⇒ **框架守卫红不了，只能项目侧判据抓**。

⚠ **本文件判据的边界（别把它读成"补了个大洞"）**：本批绑定在**效果上近乎空操作**
（除 `profile`）——六块作用域两侧都放行。它的价值在三处：
**① 让那 22 条声明从"装饰"变成"规则"；② 给判据提供那个【必须被拒的反例】（`profile`）；
③ 以后要收紧时改的是允许表、不用改代码。**
本文件①只证"**绑了**"，④/⑥ 只证"**表与意图一致 + 覆盖了所有被声明的 scope**"，
**都证不了"意图本身合理"**。
"""

from __future__ import annotations

from mecha.scopes import ScopePolicy

from paperpilot.mecha_adapter.commands import invoke_command

from tests.test_mecha_adapter import _stack


def _ai_call(stack, cmd: str, **args):
    """经 **AI 侧通道** 调命令（不走工具面：`reset_profile`/`delete_note` 本来就没投影给 AI）。"""
    return invoke_command(stack["commands"], stack["gate"], stack["channels"]["ai"],
                          cmd, {"reason": "判据", **args})


def test_scope_policy_is_bound_when_any_command_declares_scope(tmp_path):
    """① 结构性：**只要有命令声明了 `scope`，装配就必须注入 `ScopePolicy`**。

    ⚠ 读的是私有属性 `_scope_policy`：**框架没有公开 getter**（只有 `bind_scope_policy` 写口）
    ⇒ 这里如实读私有、不假装它是公开 API；若框架将来给 getter，本判据应改成用它。
    """
    _c, stack = _stack(tmp_path)
    scoped = [s.name for s in stack["commands"].specs() if s.scope]
    assert scoped, "一条声明了 scope 的命令都没有 ⇒ 本判据会空转（R8）"
    policy = getattr(stack["commands"], "_scope_policy", None)
    assert policy is not None, (
        f"有 {len(scoped)} 条命令声明了 scope，却没注入 ScopePolicy ⇒ 声明是装饰、"
        "任何通道都能调（插头没插）")


def test_ai_denied_for_profile_scope_and_allowed_for_granted(tmp_path):
    """② 双向（本批的核心，红→绿可复现）+ ③ 对偶（防"绑了就全拒"）。

    * **未绑**：AI 侧调 `reset_profile`（`scope=profile`）⇒ **静默成功** ⇒ 本判据**红**；
    * **已绑 + 未授权**：⇒ `scope_denied` ⇒ **绿**（反例就是 `profile`：防 AI 改自己的锚点）；
    * **对偶**：已绑 + 已授权（`library` 的 `star_paper`）⇒ **正常成功** ⇒ 证明不是"绑了就全拒"。
    """
    _c, stack = _stack(tmp_path)
    denied = _ai_call(stack, "reset_profile", kind="")
    assert denied["is_error"] is True, (
        "AI 侧调 reset_profile 竟然成功了 ⇒ 要么 ScopePolicy 没绑，要么 ai 被误授了 profile")
    assert denied["error"]["info"]["kind"] == "scope_denied", denied["error"]
    # ③ 对偶：已授权的作用域照常能用（library）
    ok = _ai_call(stack, "star_paper", arxiv_id="2608.01101")
    assert ok["is_error"] is False, f"library 已授权给 ai，不该被拒：{ok}"


def test_scope_policy_table_matches_intent(tmp_path):
    """④ **允许表本身要有守卫**（否则"绑歪了"没人看得见）。

    意图（用户裁决）：`library`/`topics`/`review`/`pipeline`/`config`/`undo` 两侧都放行；
    ⭐ `profile` **只给人**（"人类专属：不投影给 AI，防自改锚点"）；
    ⭐ `views` **只给人**（删视图＝处置自己的作品，2026-09-29）。
    """
    policy = getattr(_stack(tmp_path)[1]["commands"], "_scope_policy", None)
    assert isinstance(policy, ScopePolicy), "装配期应当注入 ScopePolicy"
    for scope in ("library", "topics", "review", "pipeline", "undo"):
        assert policy.allows("human", scope) is True, f"human 应当能碰 {scope}"
        assert policy.allows("ai", scope) is True, f"ai 应当能碰 {scope}（设计如此）"
    assert policy.allows("human", "profile") is True, "人当然能重置画像"
    assert policy.allows("human", "config") is True, "human 有通用配置写口"
    assert policy.allows("ai", "config") is True, "AI 有受白名单约束的配置写工具（set_config，设计如此）"
    assert policy.allows("human", "library_admin") is True, "库管理动作归人"
    assert policy.allows("ai", "library_admin") is False, "库管理动作不归 AI（管理≠使用）"
    assert policy.allows("ai", "profile") is False, (
        "ai 不该能碰 profile —— 那是「改自己的标尺」")
    assert policy.allows("human", "views") is True, "人当然能删自己画过的图"
    assert policy.allows("ai", "views") is False, (
        "ai 不该能碰 views —— 画出来的图是作品，处置权归人")
    assert policy.allows("human", "marks") is True, "人当然能删自己精读时的批注"
    assert policy.allows("ai", "marks") is False, (
        "ai 不该能碰 marks —— 批注是精读痕迹，处置权归人（撤自己刚写的走 undo）")


def test_ai_denied_for_deleting_a_mark(tmp_path):
    """删批注：**AI 侧被拒**（第二道锁）。第一道锁是不进 TOOL_DECLS，AI 工具面里根本没它。"""
    _c, stack = _stack(tmp_path)
    denied = _ai_call(stack, "delete_mark", mark_id=1)
    assert denied["is_error"] is True, "AI 侧竟然能删批注 ⇒ 作用域没生效"
    assert denied["error"]["info"]["kind"] == "scope_denied", denied["error"]


def test_ai_denied_for_deleting_a_view(tmp_path):
    """删视图：**AI 侧被拒**（第二道锁），人侧能成。

    第一道锁是"不进 TOOL_DECLS"（AI 工具面里根本没这个工具）；这里是第二道——
    就算有人绕过投影直接经 AI 通道调命令，作用域也会 fail-closed。
    """
    _c, stack = _stack(tmp_path)
    denied = _ai_call(stack, "delete_graph_view", name="随便一张")
    assert denied["is_error"] is True, "AI 侧竟然能删视图 ⇒ 作用域没生效"
    assert denied["error"]["info"]["kind"] == "scope_denied", denied["error"]


def test_every_declared_scope_is_granted_to_some_side(tmp_path):
    """⑥ 覆盖：**命令里出现过的每个 scope，都必须在允许表里至少对某一侧有授予**。

    否则"新增一条声明了新作用域的命令、却忘了配权限"⇒ **运行期才被拒**（fail-closed 的代价）
    ⇒ 这条把它变成"**跑判据就红**"。⚠ 作用域**从注册表现取**（不手抄），与允许表求差集。
    """
    stack = _stack(tmp_path)[1]
    policy = getattr(stack["commands"], "_scope_policy", None)
    assert isinstance(policy, ScopePolicy), "装配期应当注入 ScopePolicy"
    declared = {s for spec in stack["commands"].specs() for s in (spec.scope or ())}
    granted = set(policy.scopes_for("human")) | set(policy.scopes_for("ai"))
    missing = sorted(declared - granted)
    assert not missing, (
        f"这些作用域被命令声明了、却谁都没被授予 ⇒ 调用它们会在运行期被拒：{missing}")


def test_bound_policy_does_not_break_the_paths_users_actually_take(tmp_path):
    """⑤ **绑完之后，今天能做的不能坏**：把用户实际会走的路各跑一遍。

    这条是我加的（"改完会不会把活干坏"变成会红的判据，而不是靠人去想）：
    Web 的论文库写（`library`）、设置页存参数（`config`）、重置画像（`profile`，人侧）、
    记录仪撤销（`undo`）。`run_pipeline`（`pipeline`）不在这里跑（会真抓/真算）。
    """
    from fastapi.testclient import TestClient

    from paperpilot.app.container import build_container
    from paperpilot.app.web import create_app

    from tests.conftest import make_settings
    from tests.test_web_mecha import _gated

    client, _container, _stack_ = _gated(tmp_path)
    assert client.post("/papers/2608.01101/star",
                       follow_redirects=False).status_code == 303          # library（人侧）
    assert client.post("/settings/general", data={
        "lookback_days": "4", "threshold": "0.5", "quota_per_topic": "3", "max_papers": "9",
        "max_per_author": "2", "must_read_cap": "4", "review_floor": "0.3",
        "webhook_url": ""}, follow_redirects=False).status_code == 303     # config（人侧）
    assert client.post("/settings/profile/reset", data={"kind": ""},
                       follow_redirects=False).status_code == 303          # profile（人侧）
    assert client.post("/activity/undo", data={"seq": 0},
                       follow_redirects=False).status_code == 303          # undo（人侧）
    # 无栈形态不受影响（读路径与直调都不经命令面）
    plain = TestClient(create_app(build_container(make_settings(tmp_path / "plain"))))
    assert plain.get("/activity").status_code == 200

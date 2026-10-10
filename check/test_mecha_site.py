"""站点声明（`mecha_site.py`）的判据：**框架能不能按约定吃掉它** + 两处"必须钉死"的事实。

⚠ 这三条**只读 `load_sites()` 的产物**、不直接 import `mecha_site`：框架的加载路径就是
被测对象（自己 import 会绕过它，那条"能被加载"就成了空话）。
"""

from __future__ import annotations

from pathlib import Path

from mecha.launch import load_sites

REPO = Path(__file__).resolve().parents[1]


def _service():
    """框架加载本仓声明 → 取出 id=GP 的那个 service（顺带验"能被加载"）。"""
    sites = load_sites([REPO])
    found = [(s, v) for s in sites for v in s.services if v.id == "GP"]
    assert found, (
        "load_sites 没找到 id=GP 的站点（声明发现点是**仓根 mecha_site.py** 的模块级 SITE）"
        f"；实得：{[(s.name, [v.id for v in s.services]) for s in sites]}"
    )
    site, svc = found[0]
    site.validate()                       # 框架自己的校验（id 形状 / 查重 / 两腿同根 …）
    return site, svc


def test_declaration_is_loadable_and_valid():
    """A. 框架按约定吃得下：`load_sites()` + `validate()`（含 dsh 侧的补丁与产物存在）。"""
    site, svc = _service()
    assert svc.cwd == REPO
    #: ⚠ **2026-10-10 改准**（用户裁决："按项目各自安排"）：起 **`serve`** 而不是 `mcp`。
    #: GP 的人手那条腿是**浏览器里的 Web 工作台**（不是进程内窗口）⇒ `mcp` 子命令不起 Web
    #: 服务、人的腿整个消失。`serve` = 四个面同一个进程同一个栈，**照样写 `.mcp-port`**
    #: （框架的就绪判据）与 `.cockpit-port`（下面的监控面声明读它）。
    assert svc.argv[-1] == "serve", (
        f"服务那条腿必须起 `serve`（Web 人类面 + MCP + cockpit + 控制端点同进程）：{svc.argv}")
    assert "--no-open" not in svc.argv, "启动时要把人的面板带到眼前（这家的安排）；要关是它自己的选择"
    #: ⚠ 三层架构换代（2026-10-08）：`manual` 字段已被框架**删除**
    #: （"人那条腿"不再是"要 spawn 的进程"，而是**软件进程内的窗口**；写权切换不动进程）。
    #: ⇒ 旧断言 `svc.manual is None` 测的对象**已不存在**（R13：删机制时处理它的承诺）。
    #:   换成：`manual` **不许回来** + 新表达的字段**在**。
    assert not hasattr(svc, "manual"), "`manual` 已被三层架构删除，不许回来"
    assert svc.has_gui is True, "GP 的人手那条腿是 Web 界面（serve + templates/）⇒ has_gui 为 True"
    #: ⚠ 比 `.value`：`Mode` 是 `str` 枚举，`str(Mode.AI)` 给的是 `'Mode.AI'` 而不是 `'ai'`。
    #: ⭐ **2026-10-10 改准**（用户裁决）：GP 的设计里**只有一种模式** —— 人和 AI **同时**能写
    #: （项目出厂策略就是 `BOOT_MODE_DEFAULT="open"`），**没有"切到手动/交还给 AI"这回事**。
    #: ⇒ 声明 **`open`（共用档）**，框架据此把它当"**没有写权切换需求**"（启动控制台上
    #: 不画那两个假按钮）。⚠ 旧断言是 `ai`（"从 mecha 启动 = 永远 AI 组"）—— 那会让**人写的
    #: 路径被闸住**，与"只有一种模式"的设计相反。
    assert getattr(svc.initial_mode, "value", None) == "open", (
        f"GP 只有一种模式（AI 与人共用）⇒ initial_mode 应为 open：{svc.initial_mode!r}")
    from mecha.launch import LaunchController
    assert LaunchController._authority_action(svc) == "none", (
        "共用档不该在启动控制台上给「归人」的按钮（按下去也不改变谁能写）")
    for p in site.dsh.patches:
        assert p.is_file(), f"dsh 补丁不存在：{p}"
    for p in site.dsh.plugin_artifacts:
        assert p.is_file(), f"插件产物不存在（起 dsh 前必须响亮失败）：{p}"


def test_ready_port_file_matches_code(tmp_path):
    """B. 跨语言单一事实：声明的 `port_file` == 本仓代码里写端口文件用的那个名字。

    ⚠ 两边任一改名都要红：这里同时要求**声明里**与**写它的代码里**都是 `.mcp-port`。
    （不是"存在即过"——若哪天代码改用别的名字，本判据在第二半句上红。）
    """
    _site, svc = _service()
    assert svc.ready.port_file == ".mcp-port"
    assert svc.ready.ok_codes == (), (
        "实测 `GET /mcp` 回 406（该端点无 2xx 只读路由）⇒ 只能表达'有 HTTP 响应即算在听'；"
        f"实得 ok_codes={svc.ready.ok_codes!r}"
    )
    writers = [
        REPO / "src" / "paperpilot" / "app" / "cli.py",
        REPO / "src" / "paperpilot" / "mecha_adapter" / "hub.py",
    ]
    for path in writers:
        assert '".mcp-port"' in path.read_text(encoding="utf-8"), (
            f"{path.name} 里找不到 '.mcp-port' ⇒ 端口文件名在两处漂了（声明 vs 写它的代码）"
        )


def test_state_root_env_is_pinned():
    """C. §5.6：**凡影响'看到哪份状态'的环境变量必须显式钉死**（ML/EL 各踩过一次）。

    GP 的对应物是这两个（`config.py`：`PAPERPILOT_CONFIG` 决定读哪份 settings.yaml，
    `PAPERPILOT_DATA_DIR` 是状态根本身）。不钉 ⇒ 继承来的环境变量会把两腿指到不同世界，
    且**没有任何报错**。
    """
    _site, svc = _service()
    env = dict(svc.env)
    assert env.get("PAPERPILOT_CONFIG") == str(REPO / "config" / "settings.yaml")
    assert env.get("PAPERPILOT_DATA_DIR") == str(REPO / "data")
    # 钉的必须是**绝对路径**（相对路径会随 cwd 漂 —— 那正是"两腿看不同世界"的另一半）
    for key in ("PAPERPILOT_CONFIG", "PAPERPILOT_DATA_DIR"):
        assert Path(env[key]).is_absolute(), f"{key} 必须是绝对路径：{env[key]}"

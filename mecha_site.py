"""Get Paper 的**站点声明**：把"这个项目怎么被起来"交给 mecha 的统一启动器。

**它是数据，不是逻辑**：不起线程、不连网络、不读业务状态。

## 为什么每一行是现在这样（改之前先读）

| 声明 | 依据（都实测过，不是照抄别家） |
|---|---|
| `argv = <python> -m paperpilot.main serve` | ⭐ **起 `serve`**＝**四个面同一个进程同一个栈**（Web 人类面 + MCP AI 面 + cockpit 监控面 + 控制端点）：GP 的人手那条腿是**浏览器里的 Web 工作台**，`mcp` 子命令**不起 Web 服务** ⇒ 人的腿会**整个消失**（详见 `_service_argv`）。**不用 `paperpilot ai`** —— 那个前台跑 dsh，编排已归框架 |
| `ok_codes=()` | ⚠ **实测**：本仓 MCP 端点的 `GET /mcp` 回 **406**、`POST /mcp` 回 400、`/` 与 `/healthz` 回 404 ⇒ **该端口上没有任何 2xx 的只读路由**（`/healthz` 在 Web 应用上，不在这个端点）。故只能表达"**有 HTTP 响应即算在听**"（比 `tcp=True` 强：同端口可能是别的东西在听） |
| `env["PAPERPILOT_CONFIG"]` | ⚠ **决定读哪份 settings.yaml** ⇒ 决定状态根（`data_dir` 相对它上级目录解析）。不钉死 ⇒ 继承来的环境变量可能把两腿指到**不同世界**（§5.6，ML/EL 各踩过一次） |
| `env["PAPERPILOT_DATA_DIR"]` | ⚠ 状态根本身的显式值（`config.py:223` 只在 YAML 没写 `data_dir` 时生效 ⇒ 是**弱**覆盖，仍需钉，见下方"唯一不变量"） |
| `data_root = 仓根` | GP 的运行期状态全在仓内（`data/` + 三个端口文件）⇒ 取仓根（与两家同形；比 `data/` 更强：两个实例共享仓根即互斥） |
| `patches = 交付层 + 项目层` | §7.2.2 裁决：`cordis.patch.yml`（→`lib/index.mjs`）+ `cordis.isolate.patch.yml`（GP 的项目层）；**不用** `cordis.source.patch.yml`（dev 产物，与 `plugin_artifacts` 不同源） |
| `has_gui=True` | GP 的"人手那条腿"是 **Web 界面**（`serve` + `templates/`）⇒ 算有 |
| `initial_mode=Mode.AI` | 从 mecha 启动 = 永远 AI 组（与它 `BOOT_MODE_DEFAULT="open"` 的出厂策略不冲突，见下方注释） |

## ⚠ 唯一不变量（两处必须同时改）

**状态根只有一个**：`<仓根>/config/settings.yaml` 的 `data_dir`（缺省 `data` ⇒ `<仓根>/data`）。
本文件把它按缺省钉成 `<仓根>/data`。**若你把 settings.yaml 的 `data_dir` 改到别处，
必须同时改这里的 `PAPERPILOT_DATA_DIR` 与 `data_root`** —— 否则就是 ML 那种
"切了但看着是空的"（两腿看两份状态，且**没有任何报错**）。

## 查清后**没有**发现的两类（免得后人再查一遍）

* **`*_NO_*` 开关类：GP 没有**。`settings.mcp.enabled` / `settings.web.enabled` 只影响提醒与
  是否起对应服务，**不会让 `mcp` 子命令起不来**；`ai.provider=off` 也只是无 AI 档。
* **`DSH_HOME`：刻意不钉**。它只服务 `paperpilot ai --debug-home`（用项目内 `.dsh-debug`
  隔离会话/凭据）；把它钉进声明会让**每次启动都要求重新登录**，而框架自己起 dsh 时用
  共享 `~/.dsh` 即可。`--debug-home` 仍是可以手动跑的调试开关，不进声明。
* **GP 的 dsh 插件不需要任何环境变量**（与 ML 的 `MLTB_ML_ROOT` 不同）：它读的数据根来自
  `Config.projectRoot || process.cwd()`，而框架以 `cwd=仓根` 起 dsh（`dsh/src/index.ts`）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mecha.authority import Mode
from mecha.site import DSHSpec, MonitorSpec, ReadySpec, Service, Site

#: 本仓根（声明文件就在仓根）。
_SITE_ROOT = Path(__file__).resolve().parent

#: 状态根（= settings.yaml 缺省的 `data_dir`；见模块 docstring 的"唯一不变量"）。
_DATA_DIR = _SITE_ROOT / "data"

#: 真配置的路径（不钉死内容，只钉死**读哪一份**；文件不存在时 `load_settings` 照旧回落到
#: `config/settings.example.yaml`，行为与不钉时相同）。
_CONFIG_FILE = _SITE_ROOT / "config" / "settings.yaml"

#: `.mcp-port` 由 `paperpilot mcp` 写在**数据目录的父目录**（= 仓根，`hub.py`/`cli.py` 的约定）。
_PORT_FILE = ".mcp-port"


def _python() -> str:
    """起服务用哪个解释器（本声明里唯一需要判断的一处）。

    ⚠ **`paperpilot` 装在本项目的 venv 里**（README 的装法：`python -m venv .venv` + editable），
    而**框架的 `sys.executable` 可能是另一个环境**（mecha 自己的 venv）⇒ 用错解释器
    `import paperpilot` 直接失败 ⇒ **服务根本起不来**（不是"少个功能"，是整条腿都没有）。
    ⇒ 优先本项目 venv 的解释器（存在即用）；不存在才回落到 `sys.executable`
    （那种情形说明 GP 是装进框架那个环境里的，或用的是全局安装）。

    ⚠ **不用 `paperpilot.exe` 控制台脚本**：它把 venv 路径烘进 launcher、换解释器即失效；
    而 `-m paperpilot.main` 对任何"能 import paperpilot"的解释器都成立
    （`main.py` 自带 `if __name__ == "__main__"` 守卫）。
    """
    venv_python = (_SITE_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    return str(venv_python) if venv_python.is_file() else sys.executable


def _service_argv() -> tuple[str, ...]:
    """起本项目的服务：`<python> -m paperpilot.main mcp --with-web`。

    ## ⭐ 形态旗标按**词汇表**（2026-10-10 用户裁决："三家 CLI 按词汇表改名"）

    `--mcp`（起语义通道）＋ `--with-web`（**本进程同时伺服交互式网页** —— GP 的人面工作台
    就在同一个栈里）。⇒ 声明从此**不用记每家自己的方言**：EL/ML 是 `--mcp --with-gui[…]`，
    GP 是 `--mcp --with-web`，一眼看出"这一家有哪些面"。
    ⚠ `serve` 保留为**对人友好的别名**（同一套实现），但**框架声明用词汇表那套**。

    ## 为什么必须带 `--with-web`（而不是只 `mcp`）

    GP 的人手那条腿是**浏览器里的 Web 工作台**（不是进程内窗口）⇒ 它必须**有人伺服**：
    只 `mcp` 时**不起 Web 服务**（`.web-port` 永远不写）⇒ 从 mecha 启动时**人的腿根本不存在**，
    而人要在那张页面的 `/settings` 写权卡上切模式、要看流水线结果。
    `--with-web` = **四个面同一个进程同一个栈**（Web 人类面 + MCP(AI) + cockpit(监控) + 控制端点，
    见 `cli.py:_serve_impl` / `_boot_stack`）⇒ 与"该软件随 mecha 同生共死"的语义一致；
    它同样写 `.mcp-port`（框架的就绪判据不看别的）与 `.cockpit-port`（监控面声明读它）。

    ⚠ **不写别的旗标**：其余默认值就是 GP 的项目策略（host `127.0.0.1`、Web 端口取
    `settings.web.port`、cockpit 默认开、**自动开浏览器**＝"人面前台可见"）。在这里再抄一遍
    ＝同一件事两处真值（本工程反复治的病）。
    ⚠ 代价（如实记）：Web 端口是**固定**的（`settings.web.port`，缺省 8080）且被占时
    **响亮失败**（`ensure_free`，不半启动）；每次被 mecha 拉起都会**开一个浏览器标签**
    —— 那正是"启动就把人的面板带到眼前"，要关加 `--no-open`（本项目自己的选择）。
    """
    return (_python(), "-m", "paperpilot.main", "mcp", "--with-web")


#: ⚠ **两腿同源**：这两个变量一起决定"看到哪份状态"。见模块 docstring 的"唯一不变量"。
_STATE_ENV = {
    "PAPERPILOT_CONFIG": str(_CONFIG_FILE),
    "PAPERPILOT_DATA_DIR": str(_DATA_DIR),
}

SITE = Site(
    name="Get Paper",
    services=(
        Service(
            id="GP",                      # ⚠ 暴露名会小写化成 `gp_<tool>`（§2.2.1 命名律）
            brief="论文抓取 / 评审 / 简报",
            argv=_service_argv(),
            cwd=_SITE_ROOT,
            #: 就绪 = "端口文件相对 spawn 前**变过**" 且 "**探活通过**"（框架两条一起看：
            #: 缺一条就会把陈旧端口文件当成就绪的假绿）。
            #: `ok_codes=()` 的实测依据见模块 docstring（`GET /mcp` = 406）。
            ready=ReadySpec(
                port_file=_PORT_FILE,
                probe="http://127.0.0.1:{port}/mcp",
                ok_codes=(),
                timeout_s=90.0,
            ),
            data_root=_SITE_ROOT,
            #: ⭐ 三层架构（2026-10-08 换代）：**"切手动"不是另一条要 spawn 的腿**，
            #: 而是**写权切换**（``Mode``）—— 于是旧的 ``manual: ChildSpec`` 字段已从
            #: 框架删除（传它会当场 ``TypeError``，判据 ⑬ 钉住）。
            #:
            #: ``has_gui`` = "**本项目有没有人操作的那个面**" ⇒ ⭐ **GP 是 Web 界面**
            #: （``paperpilot serve`` + ``templates/``），没有桌面窗口。
            #: ⚠ 按公理 A2（"两条腿都是客户端"），**Web 面就是人手那条腿** ⇒ 算 True。
            #: ⚠ 但 GP 与别家**结构不同**：它的 Web 与 MCP **挂在同一个 stack 上**
            #: （``cli.py`` 的 ``build_stack`` + ``make_host``）⇒ 它本来就"同门"，
            #: 不需要像别家那样把窗口搬进软件进程。
            has_gui=True,
            #: ⭐ 启动者（人）声明的初始写权。⚠ GP 的既有用户裁决是
            #: ``BOOT_MODE_DEFAULT = "open"``（**"不卡写权"**：两侧都能写，代价是后写覆盖）。
            #: ⚠ 这与新架构的"**从 mecha 启动 = 永远是 AI 组**"**不冲突** ——
            #: 前者定的是"**软件出厂策略**"，后者定的是"**从 mecha 起时给什么写权**"。
            #: ⇒ 从 mecha 起 ⇒ 声明 **AI**（框架 spawn 时经 `MECHA_INITIAL_MODE` 送进去）；
            #:   ⭐ **软件侧已接通**（2026-10-08）：`hub.boot_mode` 读它并装配闸门，
            #:   优先级 **argv 显式 `--mode` > `MECHA_INITIAL_MODE` > 本仓默认 `open`**
            #:   （判据 `check/test_boot_mode_priority.py`）。
            #:   而人若想"不卡写权"，那是在**面板上切**（`Mode.OPEN`）的事。
            #: ⭐ **共用档**（2026-10-10 用户裁决）：GP 的设计里**只有一种模式** ——
            #: 人和 AI **同时**都能写（`BOOT_MODE_DEFAULT="open"` 就是它的出厂策略），
            #: **没有"切到手动/交还给 AI"这回事**。
            #: ⇒ 声明 `Mode.OPEN`，框架据此把它当"**没有写权切换需求**"：
            #: 启动控制台上**不画那两个假按钮**（`Mode.OPEN` 下两侧都能写，按钮描述不了
            #: 任何真实差别），只留真值"写权 open · AI 与人共用"。
            #: ⚠ 旧声明是 `Mode.AI` ⇒ 框架注入 `MECHA_INITIAL_MODE=ai` ⇒ 人写的路径**被闸住**，
            #: 而 GP 自己的 Web 页上又没有任何"切换"的必要（它设计上就不切）——
            #: 结果是"人要用它就得先去 /settings 解锁一次"，与项目设计相反。
            initial_mode=Mode.OPEN,
            #: ⭐ 一句话讲**人从哪个入口切写权**（框架不写死它：这是项目知识）。
            #: ⚠ 不许写端口/凭据（这段会给 AI 看，经 `read_write_authority().how_to_open`）。
            #: ⚠ 共用档下**没有"要切"的东西** ⇒ 这句如实说"无需切换"，只在真要独占/急停时
            #: 才指到它自己的 Web 页（那是**项目自己的入口**，不是框架的按钮）。
            how_to_open=("本项目 AI 与人**共用**写权（open 档）：**无需切换**。"
                         "要临时独占/急停，可在 Get Paper 的 Web 页 `/settings` 的"
                         "「写权模式」卡上切（本机可信、无口令；AI 侧不能自解锁——"
                         "它拿不到控制端点的地址）"),
            #: ⭐ **监控面声明**（三层架构 §7.3.6）：路由 / 端口文件名 / 标题只在这里写一次。
            #: ⚠ **2026-10-09 改准：`.web-port` → `.cockpit-port`**（实测抓到的真 bug）。
            #:   · 当时框架起的是 `paperpilot mcp`，那条形态下**不起 Web 服务** ⇒ `.web-port`
            #:     在那条路里**不写** ⇒ 框架那条地址路由回 **503「读不到监控端口」**、
            #:     面板那页永远打不开（⭐ 2026-10-10 已把框架起的形态改成 `serve`，两条
            #:     端口文件都写；**监控面仍然读 `.cockpit-port`**——它才是"只读审计"那个面）；
            #:   · 而 `mcp`/`serve` **都**起的是 **cockpit**（`[mecha-cockpit] http://…`，只读
            #:     `/status /activity /history /config /summary`）—— ⭐ 那正是**资产面板**
            #:     要读的那个形状（资产文档原话："监控端点的端口是动态分配的
            #:     （`mecha.cockpit` 用 `port=0` 起端点）"）。
            #:   ⇒ 「监控面」= **cockpit**；`.web-port` 是另一件事（人用的 Web 工作台，
            #:     由 `paperpilot serve` 写 —— 现在是**框架起的那个形态**在写它）。
            #: ⚠ 框架把它生成成参数、经**框架自己的 patch** 带上的插件交给浏览器
            #: ⇒ 本项目 dsh 侧只留**那一行 bootstrap**（自定义 UI 已按用户裁决删除）。
            #: ⭐ **监控地址路由也由框架注册**（框架的 host 插件按 `port_file` 的
            #: **绝对路径**现取端口）⇒ 这是那三个值的**唯一家**（项目侧的 `GP_PANEL` 已删）。
            monitor=MonitorSpec(
                route_path="/paperpilot/monitor-url",
                port_file=".cockpit-port",
                title="操作审计",
            ),
            env=_STATE_ENV,
        ),
    ),
    dsh=DSHSpec(
        command=("dsh", "web"),
        cwd=_SITE_ROOT,
        #: 入库的**交付层 + 项目层**（§7.2.2 裁决）。第三层（指向聚合端点的 overlay）
        #: 是**生成物**，由框架按聚合端点地址生成。
        patches=(
            _SITE_ROOT / "dsh" / "cordis.patch.yml",
            _SITE_ROOT / "dsh" / "cordis.isolate.patch.yml",
        ),
        #: 3080 是官方 dsh 的端口，项目实例永不占（框架从 3081 起试）。
        port_preference=3081,
        #: 插件产物：不存在 ⇒ 响亮失败（而不是起一个没有工具面的 dsh）。
        plugin_artifacts=(_SITE_ROOT / "dsh" / "lib" / "index.mjs",),
        #: GP 的插件**不需要**数据根环境变量（见模块 docstring 第三条查清项）⇒ 不编一个。
        env={},
    ),
)


__all__ = ["SITE"]

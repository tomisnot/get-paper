"""Graphviz 布局适配器：**把"谁画在哪"外包给 dot，自己只留几何**。

为什么是 Graphviz（而不是在浏览器里跑一个图 JS 库）：
- 它只**算坐标**（``-Tplain`` 给出每个点的中心/尺寸、每条边的贝塞尔控制点、边标签位置），
  渲染仍是我们自己用 HTML/SVG 画。这样悬浮卡、点节点进论文详情页、拖拽缩放、
  ``/network.json`` 回执全部留在我们手里——布局换了引擎，页面语义不换。
- 引擎在**服务端**跑：AI 交什么字，我当场就知道错没错、挤不挤，不用等人截图。
- 二进制是**仓库内 vendored**（``vendor/graphviz/<版本>/``），不污染系统 PATH，
  也不要求用户先装一遍（Windows 上尤其重要）。

⚠ 别把 ``-Tsvg`` 拿来直接贴页面：那样节点样式、悬浮去留、点击行为都被引擎接管，
回执也没了（SVG 里的坐标要再解析一遍，等于多一层反解析）。``-Tplain`` 才是数据。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

#: 仓库根（本文件在 src/paperpilot/ 下 ⇒ 上溯两层）
_REPO_ROOT = Path(__file__).resolve().parents[2]
#: vendored 位置：vendor/graphviz/<版本>/Graphviz-<版本>-win64/bin/dot.exe
_VENDOR_GLOB = ("vendor/graphviz/*/*/bin/dot.exe", "vendor/graphviz/*/*/bin/dot",
                "vendor/graphviz/*/bin/dot.exe", "vendor/graphviz/*/bin/dot")
_PT = 72.0                      # Graphviz 的 plain 输出单位是英寸，页面用 pt/px
_TIMEOUT_S = 20


def find_dot() -> str:
    """找 ``dot``：环境变量 > 仓库内 vendored > 系统 PATH。找不到返回空串。"""
    env = os.environ.get("PAPERPILOT_GRAPHVIZ", "").strip()
    if env and Path(env).exists():
        return env
    found: list[Path] = []
    for pattern in _VENDOR_GLOB:
        found.extend(_REPO_ROOT.glob(pattern))
    if found:                                     # 多个版本并存时取版本号最大的
        found.sort(key=lambda p: _version_key(p.parent.parent.parent.name), reverse=True)
        return str(found[0])
    return shutil.which("dot") or ""


def _version_key(name: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", name)) or (0,)


_CACHE: dict[str, str] = {}


def version() -> str:
    """``dot -V`` 的版本号（拿不到就空串）。只查一次。"""
    if "v" in _CACHE:
        return _CACHE["v"]
    dot = find_dot()
    ver = ""
    if dot:
        try:
            out = subprocess.run([dot, "-V"], capture_output=True, text=True,
                                 timeout=10, errors="replace")
            text = (out.stderr or "") + (out.stdout or "")
            m = re.search(r"version\s+(\S+)", text)
            ver = m.group(1) if m else ""
        except (OSError, subprocess.SubprocessError):
            ver = ""
    _CACHE["v"] = ver
    return ver


def available() -> bool:
    return bool(find_dot())


def layout(dot_text: str) -> dict:
    """跑一次 ``dot -Tplain``，把几何解析成页面能直接用的形状。

    返回 ``{ok, width, height, nodes: {id: {x,y,w,h}}, edges: [...], error}``；
    坐标单位 pt（px），**原点在左上**（Graphviz 原点在左下，这里已翻转）。
    """
    dot = find_dot()
    if not dot:
        return {"ok": False, "nodes": {}, "edges": [], "width": 0, "height": 0,
                "error": "没找到 graphviz 的 dot：跑 scripts/fetch_graphviz.ps1 装到 vendor/，"
                         "或设 PAPERPILOT_GRAPHVIZ 指向它"}
    with tempfile.TemporaryDirectory(prefix="gp-dot-") as tmp:
        src = Path(tmp) / "in.dot"
        src.write_text(dot_text, encoding="utf-8")     # 不用 stdin：Windows 控制台编码会咬人
        try:
            proc = subprocess.run([dot, "-Tplain", str(src)], capture_output=True,
                                  timeout=_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return {"ok": False, "nodes": {}, "edges": [], "width": 0, "height": 0,
                    "error": f"dot 超时（>{_TIMEOUT_S}s）：点太多或边太密"}
        except OSError as exc:
            return {"ok": False, "nodes": {}, "edges": [], "width": 0, "height": 0,
                    "error": f"dot 启动失败：{exc}"}
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        return {"ok": False, "nodes": {}, "edges": [], "width": 0, "height": 0,
                "error": f"dot 报错：{err[:800]}"}
    return _parse(proc.stdout.decode("utf-8", "replace"))


def _parse(text: str) -> dict:
    """解析 plain 输出。

    行格式（graphviz 文档 "plain output"）：
      ``graph scale width height``
      ``node name x y w h label style shape color fillcolor``
      ``edge tail head n x1 y1 … xn yn [label xl yl] style color``
    单位英寸、原点左下。
    """
    graph_w = graph_h = 0.0
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for line in text.splitlines():
        parts = _tokens(line)
        if not parts:
            continue
        tag = parts[0]
        if tag == "graph" and len(parts) >= 4:
            graph_w, graph_h = float(parts[2]), float(parts[3])
        elif tag == "node" and len(parts) >= 6:
            nid = parts[1]
            x, y, w, h = (float(parts[2]), float(parts[3]),
                          float(parts[4]), float(parts[5]))
            nodes[nid] = {"x": x * _PT, "y": y * _PT, "w": w * _PT, "h": h * _PT,
                          "label": parts[6] if len(parts) > 6 else ""}
        elif tag == "edge" and len(parts) >= 4:
            src, dst, count = parts[1], parts[2], int(parts[3])
            coords = parts[4:4 + count * 2]
            if len(coords) < count * 2:
                continue
            pts = [(float(coords[i]) * _PT, float(coords[i + 1]) * _PT)
                   for i in range(0, len(coords), 2)]
            rest = parts[4 + count * 2:]
            label, lx, ly = "", None, None
            # 有标签时 rest = [label, x, y, style, color]
            if len(rest) >= 5:
                label, lx, ly = rest[0], float(rest[1]) * _PT, float(rest[2]) * _PT
            edges.append({"src": src, "dst": dst, "points": pts, "label": label,
                          "lx": lx, "ly": ly})
    height = graph_h * _PT
    # 翻转 y：plain 原点在左下，页面（HTML/SVG）原点在左上
    for item in nodes.values():
        item["y"] = height - item["y"]
    for e in edges:
        e["points"] = [(x, height - y) for x, y in e["points"]]
        if e["ly"] is not None:
            e["ly"] = height - e["ly"]
        e["d"] = _path(e["points"])
    return {"ok": True, "width": graph_w * _PT, "height": height,
            "nodes": nodes, "edges": edges, "error": "", "version": version()}


def _path(points: list[tuple[float, float]]) -> str:
    """一串点 → ``d`` 属性：首点 M，之后每三点一段三次贝塞尔（plain 的边就是这么给的）。"""
    if not points:
        return ""
    def f(v: float) -> str:
        return f"{v:.1f}"
    out = [f"M {f(points[0][0])} {f(points[0][1])}"]
    rest = points[1:]
    for i in range(0, len(rest) - 2, 3):
        c1, c2, p = rest[i], rest[i + 1], rest[i + 2]
        out.append(f"C {f(c1[0])} {f(c1[1])} {f(c2[0])} {f(c2[1])} {f(p[0])} {f(p[1])}")
    if not out[1:]:                                  # 只有一个点（约束边）：退化成一个点
        return ""
    return " ".join(out)


def _tokens(line: str) -> list[str]:
    """按空白切，但双引号里的空白不切（标签里常有空格/中文）。"""
    out: list[str] = []
    buf: list[str] = []
    quoted = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and quoted and i + 1 < len(line):
            nxt = line[i + 1]
            buf.append("\n" if nxt in "nlr" else nxt)   # Graphviz 的左对齐/居中换行都还原成真换行
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
            i += 1
            continue
        if ch.isspace() and not quoted:
            if buf:
                out.append("".join(buf))
                buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf))
    return out

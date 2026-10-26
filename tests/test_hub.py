from __future__ import annotations

import socket

from paperpilot.mecha_adapter.hub import port_free, should_open_browser


def test_auto_opens_only_when_launched_by_framework():
    # 手动在终端跑：人就在终端里，不该凭空多一个标签页
    assert should_open_browser("auto", {}) is False
    # 被框架拉起：框架一定注入这两个变量之一
    assert should_open_browser("auto", {"MECHA_SOFTWARE_ROOT": "D:\\x"}) is True
    assert should_open_browser("auto", {"MECHA_INITIAL_MODE": "open"}) is True


def test_open_when_ready_waits_forthe_listener(monkeypatch):
    """端口还没在听就不开，在听之后**只开一次**。"""
    import threading
    import time
    import webbrowser

    from paperpilot.mecha_adapter.hub import open_when_ready

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        worker = threading.Thread(
            target=open_when_ready, args=(f"http://127.0.0.1:{port}",),
            kwargs={"host": "127.0.0.1", "port": port, "timeout": 5.0}, daemon=True)
        worker.start()
        worker.join(5.0)

    assert opened == [f"http://127.0.0.1:{port}"]

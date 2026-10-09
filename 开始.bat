@echo off
chcp 65001 >nul
cd /d %~dp0

echo ============================================
echo   PaperPilot 一键启动
echo   Web 面板 + MCP(AI 面) + 监控面 + DSH AI 界面
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython

if exist ".venv\Scripts\python.exe" goto deps

echo [1/3] 创建虚拟环境 .venv ...
python -m venv .venv
if errorlevel 1 goto venvfail

:deps
.venv\Scripts\python.exe -c "import paperpilot" >nul 2>nul
if not errorlevel 1 goto plugindeps

echo [2/3] 安装 Python 依赖（首次约 1-3 分钟，使用清华镜像加速）...
.venv\Scripts\python.exe -m pip install -e ".[dev,mecha]" -i https://pypi.tuna.tsinghua.edu.cn/simple --no-cache-dir --disable-pip-version-check --timeout 60
if errorlevel 1 goto pipfail

:plugindeps
if not exist "dsh" goto start
if exist "dsh\node_modules" goto start

echo [3/3] 安装 DSH 插件依赖（首次约 15-60s，使用 npmmirror 加速）...
pushd dsh
call npm install --legacy-peer-deps --ignore-scripts --registry=https://registry.npmmirror.com --no-audit --no-fund
popd

:start
echo.
echo ------------------------------------------------------------
echo   启动中...
echo   Web 面板:  http://127.0.0.1:8080
echo   MCP 通道:  自动端口，见 .mcp-port（dsh 自动发现）
echo   监控面:    dsh 右栏「监控」页签（原生面板 = mecha cockpit）
echo   写权切换:  启动面板上的「切到手动」/ 本项目控制端点（本机可信，无口令）
echo               —— 旧的"控制口令"已按 2026-10-08 裁决去掉
echo   AI 界面:   dsh 起来后会自动打开浏览器
echo.
echo   ⚠ 上面这条是【只跑 PaperPilot 自己】的自足路子（它自带的 dsh）。
echo      要起【整个生态】（多项目 + 一个 dsh）=> 用框架的无黑框入口：
echo        <框架仓>\start_mecha.pyw   （双击即可，不开黑框；根清单见 mecha_roots.txt）
echo.
echo   首次使用：在 dsh 里对 AI 说
echo     "看看今天的候选论文，帮我评审并生成简报"
echo ------------------------------------------------------------
echo.

.venv\Scripts\paperpilot.exe ai

echo.
echo PaperPilot 已退出。按任意键关闭窗口...
pause >nul
exit /b 0

:nopython
echo [错误] 没找到 python。请先安装 Python 3.10+ 并勾选 Add to PATH。
pause
exit /b 1

:venvfail
echo [错误] 虚拟环境创建失败。
pause
exit /b 1

:pipfail
echo [错误] Python 依赖安装失败。请检查网络后重试。
pause
exit /b 1

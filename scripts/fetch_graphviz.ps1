# 把 Graphviz 装进仓库（vendor/），不污染系统 PATH、不要求用户先装一遍。
#
# 为什么 vendored 而不是让用户自己装：Windows 上"先装个 graphviz"是整条链路上最脆的一环
# （装完没进 PATH、装在 Program Files 权限不够、版本对不上）。它只是一堆 exe+dll，
# 9.7MB 的 zip，落在仓库里就永远在那儿——`git clone` 之后跑本脚本一次即可。
#
# 用法：
#   pwsh scripts/fetch_graphviz.ps1              # 装当前钉住的版本
#   pwsh scripts/fetch_graphviz.ps1 -Version 15.1.1
#
# 装完的路径：vendor/graphviz/<版本>/Graphviz-<版本>-win64/bin/dot.exe
# 解析顺序见 src/paperpilot/infra/graphviz.py::find_dot（env > vendor > PATH）。

param(
    [string]$Version = "16.1.0",
    [string]$ProjectId = "4207231"       # gitlab.com/graphviz/graphviz
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$target = Join-Path $repo "vendor\graphviz\$Version"

$dotExe = Get-ChildItem -Path $target -Recurse -Filter dot.exe -ErrorAction SilentlyContinue |
          Select-Object -First 1
if ($dotExe) {
    Write-Host "已存在：$($dotExe.FullName)"
    & $dotExe.FullName -V
    exit 0
}

$name = "windows_10_cmake_Release_Graphviz-$Version-win64.zip"
$url = "https://gitlab.com/api/v4/projects/$ProjectId/packages/generic/graphviz-releases/$Version/$name"
$zip = Join-Path $env:TEMP $name

Write-Host "下载 $url"
if (-not (Test-Path $zip)) {
    curl.exe -sL $url -o $zip --max-time 600
}
if (-not (Test-Path $zip) -or (Get-Item $zip).Length -lt 1MB) {
    throw "下载失败或文件过小（$zip）——检查网络，或手动下载后放到该路径"
}
# 校验和（发布物同时提供 .sha256；拿到就比，比不了就只警告——不让校验成为新的阻塞点）
$sha = "$zip.sha256"
try {
    curl.exe -sL "$url.sha256" -o $sha --max-time 60
    $want = (Get-Content $sha -Raw).Trim().Split()[0]
    $got = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($want -and ($want -ne $got)) { throw "sha256 不符：期望 $want，实得 $got" }
    Write-Host "sha256 ok"
} catch {
    Write-Warning "跳过校验和比对：$($_.Exception.Message)"
}

New-Item -ItemType Directory -Force -Path $target | Out-Null
Expand-Archive -Path $zip -DestinationPath $target -Force

$dotExe = Get-ChildItem -Path $target -Recurse -Filter dot.exe | Select-Object -First 1
if (-not $dotExe) { throw "解压后没找到 dot.exe（$target）" }
Write-Host "装好了：$($dotExe.FullName)"
& $dotExe.FullName -V

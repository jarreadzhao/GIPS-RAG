# AC-78 抓包补做脚本（需要管理员权限）
#
# 用法：
#   1. 以管理员身份打开 PowerShell
#   2. 在仓库根目录执行： powershell -ExecutionPolicy Bypass -File scripts\ac78_capture.ps1
#   3. 脚本结束会打印 etl 与 txt 路径，把结果回填 reports\AC-78-抓包记录.md
#
# 说明：
#   - 非管理员执行会直接退出（pktmon 依赖 PktMon 驱动，启动需提升权限）
#   - 默认只抓 QA 链路；如需覆盖云 OCR 出域，加 -IncludeOcr 并确保 egress 已开启
#   - 本文件必须保存为 UTF-8 带 BOM：Windows PowerShell 5.1 会按 ANSI 读取无 BOM 的 .ps1，
#     中文注释被读成乱码后会引发引号失配与语法错（实测报「缺少右 }」）
#   - pktmon etl2txt 的输出是 UTF-16LE；用 Python/其它脚本解析时按 utf-16 读，按 utf-8 读会得到空结果

param(
    [int]$QaTotal = 10,
    [switch]$IncludeOcr,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$outDir = Join-Path $root 'var'
$etl = Join-Path $outDir 'ac78.etl'
$txt = Join-Path $outDir 'ac78.txt'
$python = $Python

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host '[FAIL] 当前不是管理员，pktmon 会返回「拒绝访问」（退出码 5）。' -ForegroundColor Red
    Write-Host '       请以管理员身份重新打开 PowerShell 后执行本脚本。'
    exit 5
}

New-Item -ItemType Directory -Force -Path $outDir | Out-Null
if (Test-Path $etl) { Remove-Item $etl -Force }

Write-Host '[1/4] 启动抓包 ...'
pktmon start --capture --pkt-size 128 --file-name $etl | Out-Host
if ($LASTEXITCODE -ne 0) { throw "pktmon start 失败，退出码 $LASTEXITCODE" }

try {
    Write-Host '[2/4] 执行 QA 请求 ...'
    & $python (Join-Path $root 'scripts\load_test_qa.py') --concurrency 1 --total $QaTotal `
        --out (Join-Path $outDir 'reports\ac78_load.json')

    if ($IncludeOcr) {
        Write-Host '       请另开一个终端上传一份无文本层扫描件，触发云 OCR 出域；完成后按回车继续。'
        Read-Host | Out-Null
    }
}
finally {
    Write-Host '[3/4] 停止抓包 ...'
    pktmon stop | Out-Host
}

Write-Host '[4/4] 导出可读文本 ...'
pktmon etl2txt $etl --out $txt | Out-Host

Write-Host ''
Write-Host "抓包文件：$etl"
Write-Host "文本导出：$txt"
Write-Host ''
Write-Host '判读要点：'
Write-Host '  - 过滤目标进程端口 8011 的 TCP 流，确认不存在非回环目的地址；'
Write-Host '  - QA 链路不应出现任何外部域名 / 公网 IP；'
Write-Host '  - 若开启 -IncludeOcr，应只看到云 OCR 供应商地址，且载荷为页面图像，不含条款正文；'
Write-Host '  - 把逐流统计与结论回填 reports\AC-78-抓包记录.md。'

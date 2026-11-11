# 安装带 CUDA 后端的 llama-cpp-python（Windows）
# 背景：PyPI 上的 wheel 是 CPU 版，直接 pip install 会让生成模型跑在 CPU 上，
#       8GB 卡等于白装；必须从 abetlen 的预编译索引取 cu124 版本。
# 版本下限 0.3.36：内置的 llama.cpp 才认识 Qwen3 架构，旧版会报
#       unknown model architecture: 'qwen3'。
# 注意：wheel 实体挂在 GitHub Releases 上，国内直连会 403/超时，
#       开了代理时把 -Proxy 传进来（例如 http://127.0.0.1:7890）。
param(
    [string]$CudaIndex = "https://abetlen.github.io/llama-cpp-python/whl/cu124",
    [string]$Python = "python",
    [string]$Proxy = "",
    [string]$Version = "0.3.36"
)

if ($Proxy) {
    $env:HTTP_PROXY = $Proxy
    $env:HTTPS_PROXY = $Proxy
    Write-Host "使用代理 $Proxy" -ForegroundColor Cyan
}

# 升级前先停掉占用环境的常驻服务，否则替换 numpy 等 DLL 会报 WinError 5
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*uvicorn*app.main*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Write-Host "安装 cu124 预编译 wheel ..." -ForegroundColor Cyan
# --no-deps：依赖已在环境中，强制重装会去覆盖被进程占用的 DLL
& $Python -m pip install --no-deps --upgrade "llama-cpp-python==$Version" `
    --extra-index-url $CudaIndex --only-binary=:all:

# --no-deps 装下来的包不带 python 依赖，diskcache 需要单独补
& $Python -m pip install diskcache

Write-Host "验证 CUDA 后端 ..." -ForegroundColor Cyan
# llama.dll 依赖的 cudart / cublas 由 torch 提供，验证前必须把 torch\lib 加进搜索路径
& $Python -c "import os, torch; lib = os.path.join(os.path.dirname(torch.__file__), 'lib'); os.environ['PATH'] = lib + os.pathsep + os.environ.get('PATH', ''); os.add_dll_directory(lib); import llama_cpp; from llama_cpp import llama_supports_gpu_offload; print('llama_cpp', llama_cpp.__version__, 'CUDA offload:', llama_supports_gpu_offload())"

# 启动本地基础设施并等待 Milvus 就绪
# 用法：powershell -ExecutionPolicy Bypass -File scripts/dev_up.ps1
$ErrorActionPreference = "Stop"

Write-Host "[1/3] 启动 etcd / objectstore(SeaweedFS S3) / milvus / redis ..." -ForegroundColor Cyan
docker compose up -d

Write-Host "[2/3] 等待 Milvus 健康检查通过（最长 180 秒）..." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds(180)
while ((Get-Date) -lt $deadline) {
    $status = docker inspect --format "{{.State.Health.Status}}" (docker compose ps -q milvus) 2>$null
    if ($status -eq "healthy") { break }
    Start-Sleep -Seconds 5
}

Write-Host "[3/3] 连接性自检 ..." -ForegroundColor Cyan
& python -c @"
import redis
from pymilvus import MilvusClient
r = redis.Redis.from_url('redis://127.0.0.1:6379/0')
print('redis:', r.ping())
c = MilvusClient(uri='http://127.0.0.1:19530')
print('milvus collections:', c.list_collections())
"@

param([switch]$Reset)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Demo = Join-Path $Root ".demo-data"

Push-Location $Root
try {
    $args = @("scripts\create_demo.py", "--root", $Demo)
    if ($Reset) { $args += "--reset" }
    python @args
    $env:SHIGUANG_APP_HOME = $Demo
    $env:SHIGUANG_EDITION = "general"
    Write-Host "拾光合成演示：http://127.0.0.1:18999"
    python scripts\server.py --port 18999 --open
}
finally {
    Pop-Location
}

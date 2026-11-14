# 只起记忆面板服务（不开窗口）；开机用这个
# 找 pythonw：环境变量 AAM_PYTHONW > panel-python.txt > PATH
$ErrorActionPreference = "SilentlyContinue"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 21600

$pyw = $null
if ($env:AAM_PYTHONW -and (Test-Path $env:AAM_PYTHONW)) {
    $pyw = $env:AAM_PYTHONW
}
$txt = Join-Path $dir "panel-python.txt"
if (-not $pyw -and (Test-Path $txt)) {
    $v = (Get-Content $txt -Raw).Trim()
    if ($v -and (Test-Path $v)) { $pyw = $v }
}
if (-not $pyw) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pyw = $cmd.Source }
}
if (-not $pyw) { $pyw = "pythonw" }

if ((Get-NetTCPConnection -LocalPort $port -State Listen | Measure-Object).Count -eq 0) {
    Start-Process -FilePath $pyw -ArgumentList "-X", "utf8", "$dir\webui.py", "--port", "$port" -WorkingDirectory $dir -WindowStyle Hidden
}

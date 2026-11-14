# 打开记忆面板：没在跑就先起服务，再开一个 Chrome 应用窗口
$ErrorActionPreference = "SilentlyContinue"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 21600
$url = "http://127.0.0.1:$port/"

function Resolve-Pythonw {
    if ($env:AAM_PYTHONW -and (Test-Path $env:AAM_PYTHONW)) { return $env:AAM_PYTHONW }
    $txt = Join-Path $dir "panel-python.txt"
    if (Test-Path $txt) {
        $v = (Get-Content $txt -Raw).Trim()
        if ($v -and (Test-Path $v)) { return $v }
    }
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
                     "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe")) {
        if (Test-Path $c) { return $c }
    }
    return "pythonw"
}

function Resolve-Chrome {
    if ($env:AAM_CHROME -and (Test-Path $env:AAM_CHROME)) { return $env:AAM_CHROME }
    foreach ($c in @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
                     "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                     "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
                     "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe")) {
        if (Test-Path $c) { return $c }
    }
    $cmd = Get-Command chrome.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return "chrome"
}

$listening = (Get-NetTCPConnection -LocalPort $port -State Listen | Measure-Object).Count -gt 0
if (-not $listening) {
    $pyw = Resolve-Pythonw
    Start-Process -FilePath $pyw -ArgumentList "-X", "utf8", "$dir\webui.py", "--port", "$port" -WorkingDirectory $dir -WindowStyle Hidden
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 400
        if ((Get-NetTCPConnection -LocalPort $port -State Listen | Measure-Object).Count -gt 0) { break }
    }
}

$chrome = Resolve-Chrome
Start-Process -FilePath $chrome -ArgumentList "--app=$url", "--window-size=1400,900"

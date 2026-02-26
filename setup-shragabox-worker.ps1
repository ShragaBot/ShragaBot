# Shraga Worker Box Setup (additional Shraga Box — SW only, no PS)
# Use this for your 2nd, 3rd, etc. Shraga Boxes. PS stays on your main box.
#
# Usage: irm https://raw.githubusercontent.com/ShragaBot/ShragaBot/main/setup-shragabox-worker.ps1 | iex

$scriptUrl = "https://raw.githubusercontent.com/ShragaBot/ShragaBot/main/setup-shragabox.ps1"
$localScript = Join-Path $env:TEMP "setup-shragabox.ps1"
Invoke-WebRequest -Uri $scriptUrl -OutFile $localScript -ErrorAction Stop
& powershell -ExecutionPolicy Bypass -File $localScript -WorkerOnly

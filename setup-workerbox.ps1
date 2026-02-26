# Shraga Worker Box Setup (additional Shraga Box — SW only, no PS)
# Use this for your 2nd, 3rd, etc. Shraga Boxes. PS stays on your main box.
#
# Usage: irm https://raw.githubusercontent.com/ShragaBot/ShragaBot/main/setup-workerbox.ps1 | iex

$scriptUrl = "https://raw.githubusercontent.com/ShragaBot/ShragaBot/main/setup-devbox.ps1"
$localScript = Join-Path $env:TEMP "setup-devbox.ps1"
Invoke-WebRequest -Uri $scriptUrl -OutFile $localScript -ErrorAction Stop
& powershell -ExecutionPolicy Bypass -File $localScript -WorkerOnly

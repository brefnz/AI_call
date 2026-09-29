# start-all.ps1
# Jalankan dari root folder project (sebelah main.py, config.py, api_server.py):
#   powershell -ExecutionPolicy Bypass -File start-all.ps1
#
# Buka 2 window PowerShell baru:
#   1. main.py  -- log dibuang ke app.log (terminal diam)
#   2. uvicorn api_server -- log tetap tampil di window-nya sendiri
# Window asli tempat kamu jalanin script ini bisa dipakai buat Get-Content -Wait
# kalau mau mantau log, atau ditutup aja.

$root = $PSScriptRoot

Write-Host "Starting main.py (log -> app.log)..."
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root'; venv\Scripts\activate; python main.py *>> app.log"
)

Start-Sleep -Seconds 1

Write-Host "Starting uvicorn (api_server) di port 8090..."
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root'; venv\Scripts\activate; uvicorn api_server:app --reload --port 8090"
)

Write-Host ""
Write-Host "Kedua proses jalan di window terpisah."
Write-Host "Buka dashboard.html di browser, atau pantau log dengan:"
Write-Host "  Get-Content app.log -Wait -Tail 20"

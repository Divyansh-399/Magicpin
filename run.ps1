param([switch]$Test)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path '.venv')) {
    Write-Host 'Creating virtual environment...'
    python -m venv .venv
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
Write-Host 'Installing dependencies...'
& $python -m pip install --quiet -r requirements.txt

if (-not (Test-Path 'expanded')) {
    Write-Host 'Generating expanded dataset (50 merchants / 200 customers / 100 triggers)...'
    & $python generate_dataset.py --seed-dir . --out expanded
}

& $python build_single_file.py
Write-Host 'Starting the bot on http://localhost:8080 ...'
$server = Start-Process -FilePath $python -ArgumentList 'vera_bot_single_file.py' -PassThru -WindowStyle Hidden

try {
    Start-Sleep -Seconds 2
    if ($Test) {
        Write-Host 'Running the local harness...'
        & $python run_local_harness.py
    } else {
        Wait-Process -Id $server.Id
    }
} finally {
    if ($Test -and -not $server.HasExited) {
        Stop-Process -Id $server.Id
    }
}

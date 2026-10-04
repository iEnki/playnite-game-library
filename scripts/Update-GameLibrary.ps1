<#
  Update-GameLibrary.ps1
  Uploads the current Playnite HTML export to your Game Library website.
  The server fills missing covers, rebuilds the page and (optionally) creates AI texts.

  Settings (parameters or environment variables):
    -Source  / GAMELIB_EXPORT : folder of the Playnite HTML export
    -Url     / GAMELIB_URL    : address of the website, e.g. http://192.168.1.20:8095
    -Token   / GAMELIB_TOKEN  : UPDATE_TOKEN from the stack

  Store the settings once (PowerShell, current user):
    [Environment]::SetEnvironmentVariable('GAMELIB_EXPORT', 'C:\Users\me\Downloads\My Games', 'User')
    [Environment]::SetEnvironmentVariable('GAMELIB_URL',    'http://192.168.1.20:8095', 'User')
    [Environment]::SetEnvironmentVariable('GAMELIB_TOKEN',  '<your token>', 'User')

  Run:
    powershell -ExecutionPolicy Bypass -File .\Update-GameLibrary.ps1
#>
param(
    [string]$Source = $env:GAMELIB_EXPORT,
    [string]$Url    = $env:GAMELIB_URL,
    [string]$Token  = $env:GAMELIB_TOKEN
)

$ErrorActionPreference = 'Stop'
if (-not $Source) { throw 'Export folder missing: use -Source or set GAMELIB_EXPORT.' }
if (-not $Url)    { throw 'Website address missing: use -Url or set GAMELIB_URL.' }
if (-not $Token)  { throw 'Token missing: use -Token or set GAMELIB_TOKEN.' }
$Url = $Url.TrimEnd('/')
if (-not (Test-Path (Join-Path $Source 'index.html'))) {
    throw "No Playnite export found: $Source\index.html is missing."
}

$zip = Join-Path $env:TEMP 'playnite-game-library-export.zip'
if (Test-Path $zip) { Remove-Item $zip -Force }

Write-Host "Packing export '$Source' ..." -ForegroundColor Cyan
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($Source, $zip, [System.IO.Compression.CompressionLevel]::Fastest, $false)
$sizeMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)

Write-Host "Uploading ($sizeMb MB) to $Url ..." -ForegroundColor Cyan
try {
    Invoke-RestMethod -Method Post -Uri "$Url/api/import" -InFile $zip `
        -ContentType 'application/zip' -Headers @{ 'X-Token' = $Token } -TimeoutSec 1800 | Out-Null
} catch {
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    throw "Upload failed: $($_.Exception.Message)"
}
Remove-Item $zip -Force

Write-Host 'Upload ok - server is processing the export:' -ForegroundColor Cyan
$shown = 0
do {
    Start-Sleep -Seconds 2
    $s = Invoke-RestMethod -Uri "$Url/api/status" -Headers @{ 'X-Token' = $Token } -TimeoutSec 30
    $lines = @($s.log)
    if ($lines.Count -gt $shown) { $lines[$shown..($lines.Count - 1)] | ForEach-Object { Write-Host "  $_" }; $shown = $lines.Count }
} while ($s.state -eq 'running')

if ($s.state -eq 'done') {
    Write-Host ("Done: {0} games, {1} covers added." -f $s.summary.games, $s.summary.covers_filled) -ForegroundColor Green
    if ($s.ai -and $s.ai.state -eq 'running') { Write-Host 'AI texts are being generated in the background (log in as admin on the website to follow progress).' }
} else {
    Write-Host 'Import failed - see log above.' -ForegroundColor Red
    exit 1
}

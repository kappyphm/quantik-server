param([switch]$EnableProvider)
$ErrorActionPreference = "Stop"
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
$taskPython = Join-Path $HOME ".venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $taskPython)) { throw "Shared Python environment is missing: $taskPython" }
$env:PYTHONPATH = Join-Path $taskRoot "backend/src"
if ($EnableProvider) { $env:QUANTIK_SCHEDULED_PROVIDER_ENABLED = "true" }
$taskLogs = Join-Path $taskRoot "runtime/screening-logs"
New-Item -ItemType Directory -Force -Path $taskLogs | Out-Null
foreach ($taskService in @(@{name="scheduler";arguments=@("scheduler")}, @{name="ingest";arguments=@("schedule-worker","--kind","INGEST")}, @{name="screen";arguments=@("schedule-worker","--kind","SCREEN")})) {
    $taskSuffix = "service.cli " + ($taskService.arguments -join " ")
    $taskExisting = Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $taskPython -and $_.CommandLine -like "*$taskSuffix*" }
    if ($taskExisting) { Write-Output "$($taskService.name): already running"; continue }
    $taskProcess = Start-Process -FilePath $taskPython -ArgumentList (@("-u","-m","service.cli") + $taskService.arguments) -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskLogs "$($taskService.name).out.log") -RedirectStandardError (Join-Path $taskLogs "$($taskService.name).err.log")
    Write-Output "$($taskService.name): PID $($taskProcess.Id)"
}
Write-Output "Provider ingestion also requires OPERATIONS.enabled and ingestion_enabled. Logs: $taskLogs"

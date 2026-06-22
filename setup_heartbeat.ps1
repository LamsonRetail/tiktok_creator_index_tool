# Đăng ký 3 task Windows Task Scheduler để chạy heartbeat.py định kỳ
# với RandomDelay (ngẫu nhiên hoá thời điểm thực sự kích hoạt).
#
# Cách dùng:  mở PowerShell, cd vào thư mục dự án, chạy:
#   powershell -ExecutionPolicy Bypass -File .\setup_heartbeat.ps1
#
# Gỡ bỏ:   .\setup_heartbeat.ps1 -Remove

param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script     = Join-Path $ProjectDir "heartbeat.py"
$PythonExe  = "C:\Users\PC\AppData\Local\Programs\Python\Python311\python.exe"

$TaskNames = @(
    "TikTokCrawl_Heartbeat_Night",
    "TikTokCrawl_Heartbeat_Noon",
    "TikTokCrawl_Heartbeat_Random"
)

if ($Remove) {
    foreach ($n in $TaskNames) {
        try {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction Stop
            Write-Host "Removed: $n"
        } catch {
            Write-Host "Skip (not found): $n"
        }
    }
    return
}

if (-not (Test-Path $PythonExe)) {
    Write-Error "Khong tim thay python tai: $PythonExe"
    return
}
if (-not (Test-Path $Script)) {
    Write-Error "Khong tim thay heartbeat.py tai: $Script"
    return
}

function Register-HeartbeatTask {
    param(
        [string]$Name,
        [string]$StartTime,        # vd "01:30"
        [string]$RandomDelay,      # ISO8601 vd "PT3H" = random 0..3h
        [string]$Description
    )

    $action = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument "`"$Script`"" `
        -WorkingDirectory $ProjectDir

    # New-ScheduledTaskTrigger -Daily khong ho tro -RandomDelay truc tiep,
    # nen tao bang CIM class va set RandomDelay = chuoi ISO8601 (vd "PT3H").
    $trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
    $cimTrig = Get-CimClass `
        -ClassName MSFT_TaskDailyTrigger `
        -Namespace Root/Microsoft/Windows/TaskScheduler
    $trigger = New-CimInstance -CimClass $cimTrig -ClientOnly -Property @{
        Enabled       = $true
        StartBoundary = (Get-Date $StartTime).ToString("yyyy-MM-ddTHH:mm:ss")
        DaysInterval  = [uint16]1
        RandomDelay   = $RandomDelay
    }

    # Settings: chay duoc ca khi may chay bang pin, wake may neu can,
    # bo qua neu da co instance dang chay, dung sau 10 phut neu treo.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -WakeToRun `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
        -MultipleInstances IgnoreNew

    # Chay duoi user hien tai, kg can elevation, kg can dang nhap (S4U)
    $principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType S4U `
        -RunLevel Limited

    $task = New-ScheduledTask `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $Description

    Register-ScheduledTask -TaskName $Name -InputObject $task -Force | Out-Null
    Write-Host "Registered: $Name  (start=$StartTime, +random $RandomDelay)"
}

Register-HeartbeatTask `
    -Name "TikTokCrawl_Heartbeat_Night" `
    -StartTime "01:30" `
    -RandomDelay "PT3H" `
    -Description "TikTok session keep-alive (random 01:30-04:30)"

Register-HeartbeatTask `
    -Name "TikTokCrawl_Heartbeat_Noon" `
    -StartTime "12:00" `
    -RandomDelay "PT1H" `
    -Description "TikTok session keep-alive (random 12:00-13:00)"

Register-HeartbeatTask `
    -Name "TikTokCrawl_Heartbeat_Random" `
    -StartTime "09:00" `
    -RandomDelay "PT7H" `
    -Description "TikTok session keep-alive (random 09:00-16:00)"

Write-Host ""
Write-Host "Done. Mo Task Scheduler de xem cac task 'TikTokCrawl_Heartbeat_*'."
Write-Host "Log heartbeat ghi tai: $ProjectDir\heartbeat.log"

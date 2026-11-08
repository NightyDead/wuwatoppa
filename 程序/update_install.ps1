param(
    [Parameter(Mandatory=$true)][string]$Install,
    [Parameter(Mandatory=$true)][int]$ParentPid
)
$ErrorActionPreference = 'Stop'
$installRoot = [IO.Path]::GetFullPath($Install)
$stage = [IO.Path]::GetFullPath($PSScriptRoot)
if ((Split-Path -Parent $stage) -ne (Split-Path -Parent $installRoot) -or
    (Split-Path -Leaf $stage) -notmatch '^\.wuwatoppa-update-[a-zA-Z0-9_-]+$') {
    throw 'Invalid updater staging directory.'
}
# Build the executable name using code points for Windows PowerShell 5.1.
$appName = -join (0x684C,0x5BA0,0x52A9,0x624B | ForEach-Object { [char]$_ })
$exeName = "$appName.exe"
$liveExe = Join-Path $installRoot $exeName
$liveInternal = Join-Path $installRoot '_internal'
$new = Join-Path $stage 'new'
$backup = Join-Path $stage 'backup'
$backupExe = Join-Path $backup $exeName
$backupInternal = Join-Path $backup '_internal'
$log = Join-Path $stage 'install.log'
$installedExe = $installedInternal = $savedExe = $savedInternal = $false
$process = $ready = $null
$committed = $false
$failed = $false

function Write-UpdateLog([string]$Message) {
    try { Add-Content -LiteralPath $log -Value $Message -Encoding UTF8 } catch {}
}

function Get-OrdinaryItem([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing to follow a reparse point: $Path"
    }
    return $item
}

function Copy-UserData([string]$Source, [string]$Destination) {
    if (!(Test-Path -LiteralPath $Source)) { return }
    $item = Get-OrdinaryItem $Source
    if ($item.PSIsContainer) {
        [IO.Directory]::CreateDirectory($Destination) | Out-Null
        foreach ($entry in Get-ChildItem -LiteralPath $Source -Force) {
            Copy-UserData $entry.FullName (Join-Path $Destination $entry.Name)
        }
    } else {
        [IO.Directory]::CreateDirectory((Split-Path -Parent $Destination)) | Out-Null
        [IO.File]::Copy($Source, $Destination, $false)
    }
}

function Remove-UpdateTree([string]$Path) {
    foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($Path)) {
        $attributes = [IO.File]::GetAttributes($entry)
        if ($attributes -band [IO.FileAttributes]::Directory) {
            if ($attributes -band [IO.FileAttributes]::ReparsePoint) {
                [IO.Directory]::Delete($entry)
            } else {
                Remove-UpdateTree $entry
            }
        } else {
            [IO.File]::Delete($entry)
        }
    }
    [IO.Directory]::Delete($Path)
}

try {
    $parent = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
    if ($parent -and !$parent.WaitForExit(30000)) {
        throw 'The assistant did not exit in time.'
    }
    foreach ($path in @($installRoot, $stage, $liveInternal, $liveExe, (Join-Path $liveInternal 'collector'))) {
        $null = Get-OrdinaryItem $path
    }
    [IO.Directory]::CreateDirectory($backup) | Out-Null
    $paths = Get-Content -LiteralPath (Join-Path $stage 'preserve.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($relative in $paths) {
        $source = [IO.Path]::GetFullPath((Join-Path $liveInternal $relative))
        $destination = [IO.Path]::GetFullPath((Join-Path $new "_internal/$relative"))
        if (!$source.StartsWith($liveInternal + '\', [StringComparison]::OrdinalIgnoreCase) -or
            !$destination.StartsWith((Join-Path $new '_internal') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Invalid user-data path.'
        }
        Copy-UserData $source $destination
    }
    [IO.File]::Move($liveExe, $backupExe)
    $savedExe = $true
    [IO.Directory]::Move($liveInternal, $backupInternal)
    $savedInternal = $true
    [IO.Directory]::Move((Join-Path $new '_internal'), $liveInternal)
    $installedInternal = $true
    [IO.File]::Move((Join-Path $new $exeName), $liveExe)
    $installedExe = $true

    $token = [guid]::NewGuid().ToString('N')
    $ready = New-Object System.Threading.EventWaitHandle($false, 'ManualReset', "Local\WuwaToppaUpdate-$token")
    $env:WUWATOPPA_UPDATE_READY = $token
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    try {
        $process = Start-Process -FilePath $liveExe -WorkingDirectory $installRoot -PassThru
    } finally {
        Remove-Item Env:WUWATOPPA_UPDATE_READY -ErrorAction SilentlyContinue
    }
    $started = $false
    for ($i = 0; $i -lt 150; $i++) {
        if ($ready.WaitOne(200)) { $started = $true; break }
        if ($process.HasExited) { break }
    }
    if (!$started) { throw 'The updated assistant did not report startup.' }
    $committed = $true
} catch {
    $failed = $true
    Write-UpdateLog $_.Exception.Message
    try {
        if ($process -and !$process.HasExited) {
            $process.Kill()
            if (!$process.WaitForExit(5000)) { throw 'Updated assistant could not be stopped.' }
        }
        if ($installedExe) { [IO.File]::Move($liveExe, (Join-Path $new $exeName)) }
        if ($installedInternal) { [IO.Directory]::Move($liveInternal, (Join-Path $new '_internal')) }
        if ($savedInternal) { [IO.Directory]::Move($backupInternal, $liveInternal) }
        if ($savedExe) { [IO.File]::Move($backupExe, $liveExe) }
        if (Test-Path -LiteralPath $liveExe) {
            if (!$parent -or $parent.HasExited) {
                $env:WUWATOPPA_UPDATE_FAILED = '1'
                $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
                Start-Process -FilePath $liveExe -WorkingDirectory $installRoot | Out-Null
            }
        }
    } catch {
        Write-UpdateLog "Rollback failed. Backup retained at $backup. $($_.Exception.Message)"
    }
} finally {
    if ($ready) { $ready.Dispose() }
}
if ($committed) {
    Set-Location -LiteralPath (Split-Path -Parent $stage)
    try { $null = Get-OrdinaryItem $stage; Remove-UpdateTree $stage } catch {}
}
if ($failed) { exit 1 }

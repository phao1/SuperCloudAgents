param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'down', 'status', 'logs', 'restart-gateway')]
    [string]$Command = 'status',
    [Parameter(Position = 1)]
    [string]$Service
)

$ErrorActionPreference = 'Stop'
$Script = Join-Path $PSScriptRoot 'portal_source.py'
$VenvPython = Join-Path (Split-Path $PSScriptRoot -Parent) '.venv\Scripts\python.exe'
$PythonArgs = @()
if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
    $Python = $VenvPython
} elseif ($Launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue) {
    $Python = $Launcher.Source
    $PythonArgs = @('-3.12')
} elseif ($Installed = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue) {
    $Python = $Installed.Source
} else {
    Write-Error '需要 Python 3.12，请运行 winget install -e --id Python.Python.3.12。'
}

try {
    $Version = & $Python @PythonArgs --version 2>$null
} catch {
    $Version = $null
}
if ($LASTEXITCODE -ne 0 -or $Version -notmatch '^Python 3\.12\.') {
    Write-Error '需要 Python 3.12，请运行 winget install -e --id Python.Python.3.12。'
}
$Arguments = @('-X', 'utf8', $Script, $Command)
if ($Service) {
    $Arguments += $Service
}
& $Python @PythonArgs @Arguments
exit $LASTEXITCODE

[CmdletBinding()]
param(
    [ValidateSet("auto", "uv", "pipx", "pip")]
    [string]$Method = "auto",
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }),
    [switch]$SkipCli,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$skillSource = Join-Path $repoRoot "skills\instsci"
$resolvedCodexHome = [System.IO.Path]::GetFullPath($CodexHome)
$skillsRoot = [System.IO.Path]::GetFullPath((Join-Path $resolvedCodexHome "skills"))
$skillDestination = [System.IO.Path]::GetFullPath((Join-Path $skillsRoot "instsci"))
$skillsPrefix = $skillsRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar

if (-not (Test-Path -LiteralPath (Join-Path $skillSource "SKILL.md"))) {
    throw "InstSci skill source was not found at: $skillSource"
}
if (-not $skillDestination.StartsWith($skillsPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to install outside the configured Codex skills directory: $skillDestination"
}
if ((Test-Path -LiteralPath $skillDestination) -and -not $Force -and -not $DryRun) {
    throw "Skill destination already exists. Re-run with -Force to replace it: $skillDestination"
}
if (-not $SkipCli -and -not $Force -and -not $DryRun -and (Get-Command instsci -ErrorAction SilentlyContinue)) {
    throw "InstSci CLI is already installed. Re-run with -Force to replace or upgrade it."
}

function Invoke-InstallCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host ("CLI: {0} {1}" -f $Executable, ($Arguments -join " "))
    if (-not $DryRun) {
        & $Executable @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "CLI installation failed with exit code $LASTEXITCODE."
        }
    }
}

if (-not $SkipCli) {
    $selectedMethod = $Method
    if ($selectedMethod -eq "auto") {
        if (Get-Command uv -ErrorAction SilentlyContinue) {
            $selectedMethod = "uv"
        } elseif (Get-Command pipx -ErrorAction SilentlyContinue) {
            $selectedMethod = "pipx"
        } else {
            $selectedMethod = "pip"
        }
    }

    switch ($selectedMethod) {
        "uv" {
            $arguments = @("tool", "install")
            if ($Force) { $arguments += "--force" }
            $arguments += $repoRoot
            Invoke-InstallCommand -Executable "uv" -Arguments $arguments
        }
        "pipx" {
            $arguments = @("install")
            if ($Force) { $arguments += "--force" }
            $arguments += $repoRoot
            Invoke-InstallCommand -Executable "pipx" -Arguments $arguments
        }
        "pip" {
            $python = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { $null }
            if (-not $python) { throw "Python 3.10+ is required when uv and pipx are unavailable." }
            $arguments = @("-m", "pip", "install", "--user")
            if ($Force) { $arguments += "--upgrade" }
            $arguments += $repoRoot
            Invoke-InstallCommand -Executable $python -Arguments $arguments
        }
    }
}

Write-Host "Skill: $skillSource -> $skillDestination"
if (-not $DryRun) {
    New-Item -ItemType Directory -Path $skillsRoot -Force | Out-Null
    if (Test-Path -LiteralPath $skillDestination) {
        if (-not $Force) {
            throw "Skill destination already exists. Re-run with -Force to replace it: $skillDestination"
        }
        Remove-Item -LiteralPath $skillDestination -Recurse -Force
    }
    Copy-Item -LiteralPath $skillSource -Destination $skillDestination -Recurse
}

if ($DryRun) {
    Write-Host "Dry run complete; no files or environments were changed."
} else {
    Write-Host "InstSci installation complete. Restart Codex if the skill is not discovered immediately."
}

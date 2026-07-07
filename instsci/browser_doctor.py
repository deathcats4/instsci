"""Visible CloakBrowser inspection for InstSci browser workflows.

This module intentionally observes only. It does not click page controls,
enter credentials, close windows, or stop broker processes.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import platform
import subprocess
from typing import Any


def inspect_cloakbrowser(
    *,
    output_dir: str | Path,
    publisher: str = "",
    capture_screenshots: bool = True,
    focus_windows: bool = True,
    browser_profile: str = "",
) -> dict[str, Any]:
    """Inspect visible InstSci CloakBrowser windows and write a JSON report.

    Windows gets full UIAutomation + screenshot support through a short
    PowerShell helper. Other platforms currently return a clear unsupported
    report so callers can fail gracefully instead of guessing.
    """

    out = Path(output_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    if platform.system().lower() != "windows":
        report = {
            "generated_at": datetime.now().isoformat(),
            "publisher": publisher,
            "output_dir": str(out),
            "state": "unsupported_platform",
            "recommendation": "browser-doctor screenshot inspection is currently implemented for Windows visible desktops.",
            "cloakbrowser_process_count": 0,
            "window_count": 0,
            "matching_window_count": 0,
            "profile_filter": browser_profile,
            "windows": [],
            "capture_screenshots": capture_screenshots,
            "focus_windows": focus_windows,
        }
        _write_report(out, report)
        return report

    return _inspect_windows_with_powershell(
        out,
        publisher,
        capture_screenshots=capture_screenshots,
        focus_windows=focus_windows,
        browser_profile=browser_profile,
    )


def _write_report(output_dir: Path, report: dict[str, Any]) -> None:
    (output_dir / "inspection.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _inspect_windows_with_powershell(
    output_dir: Path,
    publisher: str,
    *,
    capture_screenshots: bool,
    focus_windows: bool,
    browser_profile: str,
) -> dict[str, Any]:
    script_path = output_dir / "_browser_doctor_probe.ps1"
    script_path.write_text(_POWERSHELL_PROBE, encoding="utf-8")
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-OutputDir",
        str(output_dir),
        "-Publisher",
        publisher,
        "-CaptureScreenshots",
        "1" if capture_screenshots else "0",
        "-FocusWindows",
        "1" if focus_windows else "0",
        "-BrowserProfile",
        browser_profile,
    ]
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    report_path = output_dir / "inspection.json"
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        except Exception:
            report = {}
    else:
        report = {}
    if not report and proc.stdout.strip():
        try:
            report = json.loads(proc.stdout)
        except Exception:
            report = {}

    if not report:
        report = {
            "generated_at": datetime.now().isoformat(),
            "publisher": publisher,
            "output_dir": str(output_dir),
            "state": "probe_failed",
            "recommendation": "PowerShell browser probe did not produce inspection.json.",
            "cloakbrowser_process_count": 0,
            "window_count": 0,
            "matching_window_count": 0,
            "profile_filter": browser_profile,
            "windows": [],
        }
    if proc.returncode != 0:
        report["probe_returncode"] = proc.returncode
        report["probe_stderr"] = proc.stderr[-4000:]
        if report.get("state") == "no_window":
            report["state"] = "probe_failed"
    report.setdefault("probe_stdout", proc.stdout[-4000:] if proc.stdout else "")
    _write_report(output_dir, report)
    return report


_POWERSHELL_PROBE = r'''
param(
  [string]$OutputDir,
  [string]$Publisher = "",
  [int]$CaptureScreenshots = 1,
  [int]$FocusWindows = 1,
  [string]$BrowserProfile = ""
)

$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

function Get-CommandLineProfile {
  param([string]$CommandLine)
  if ($CommandLine -match '--user-data-dir="?([^"\s]+[^"]*?)"?(?:\s|$)') {
    return $Matches[1]
  }
  return ""
}

function Normalize-PathForCompare {
  param([string]$PathValue)
  if ([string]::IsNullOrWhiteSpace($PathValue)) { return "" }
  try {
    return [System.IO.Path]::GetFullPath($PathValue).TrimEnd("\").ToLowerInvariant()
  } catch {
    return $PathValue.TrimEnd("\").ToLowerInvariant()
  }
}

function Get-WindowElement {
  param([IntPtr]$Handle)
  try {
    Add-Type -AssemblyName UIAutomationClient -ErrorAction SilentlyContinue
    Add-Type -AssemblyName UIAutomationTypes -ErrorAction SilentlyContinue
    return [System.Windows.Automation.AutomationElement]::FromHandle($Handle)
  } catch {
    return $null
  }
}

function Get-InterestingText {
  param($Window)
  $items = New-Object System.Collections.Generic.List[string]
  if (-not $Window) { return @() }
  try {
    $all = $Window.FindAll(
      [System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition
    )
    for ($i = 0; $i -lt $all.Count; $i++) {
      $e = $all.Item($i)
      $name = [string]$e.Current.Name
      if ([string]::IsNullOrWhiteSpace($name)) { continue }
      if ($name.Length -gt 240) { $name = $name.Substring(0, 240) }
      if ($name -match "about:blank|https?://|Access|provided by|organization|institution|subscription|Open PDF|PDF|Download|robot|captcha|Cloudflare|problem|CPE|Purchase|View PDF|Full text|Sign in|error") {
        if (-not $items.Contains($name)) { $items.Add($name) | Out-Null }
      }
    }
  } catch {}
  return @($items)
}

function Get-AddressBarValue {
  param($Window)
  if (-not $Window) { return "" }
  try {
    $all = $Window.FindAll(
      [System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition
    )
    for ($i = 0; $i -lt $all.Count; $i++) {
      $e = $all.Item($i)
      $name = [string]$e.Current.Name
      $type = [string]$e.Current.ControlType.ProgrammaticName
      if ($type -notmatch 'Edit|ComboBox') { continue }
      $value = ""
      try {
        $vp = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $value = [string]$vp.Current.Value
      } catch {}
      if ($value -match '^about:blank$|^https?://') { return $value }
      if ($name -match '^about:blank$|^https?://') { return $name }
    }
  } catch {}
  return ""
}

function Save-WindowScreenshot {
  param(
    [object]$Window,
    [IntPtr]$Handle,
    [string]$Path,
    [bool]$FocusWindow = $true
  )
  if (-not $Window) { return $false }
  try {
    Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class InstSciDoctorWin32 {
  [DllImport("user32.dll")]
  public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")]
  public static extern bool SetForegroundWindow(IntPtr hWnd);
}
"@ -ErrorAction SilentlyContinue
    if ($FocusWindow) {
      [InstSciDoctorWin32]::ShowWindow($Handle, 9) | Out-Null
      [InstSciDoctorWin32]::SetForegroundWindow($Handle) | Out-Null
      Start-Sleep -Milliseconds 300
    }
    $rect = $Window.Current.BoundingRectangle
    if ($rect.IsEmpty -or $rect.Width -lt 10 -or $rect.Height -lt 10) { return $false }
    $bmp = New-Object System.Drawing.Bitmap([int]$rect.Width, [int]$rect.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bmp)
    $graphics.CopyFromScreen([int]$rect.X, [int]$rect.Y, 0, 0, $bmp.Size)
    $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose()
    $bmp.Dispose()
    return $true
  } catch {
    return $false
  }
}

function Classify-State {
  param(
    [string]$Title,
    [string]$Url,
    [string[]]$Texts,
    [int]$WindowCount
  )
  $joined = (($Texts + @($Title, $Url)) -join "`n")
  if ($WindowCount -eq 0) { return "no_window" }
  if ($Url -eq "about:blank" -or ($Title -match "^about:blank|^Chromium$" -and $joined -notmatch "https?://|PDF|Access|robot|problem|CPE")) { return "blank" }
  if ($joined -match "There was a problem providing the content|CPE\d+|content you requested|problem providing the content") { return "publisher_error" }
  if ($joined -match "Cloudflare|Ray ID|DDoS|security check|temporarily unavailable|too many requests|unusual traffic|blocked by security|Attention Required") { return "waf_blocked" }
  if ($joined -match "confirm you are human|verify you are human|checking if the site connection is secure|checking your browser|press and hold|checkbox|robot check|Are you a robot|captcha|Turnstile|human verification|请完成验证|人机验证|安全验证") { return "human_verification_required" }
  if ($joined -match "Access denied|not entitled|no access|your institution does not have access|purchase access|Purchase PDF|get access|rent this article|无权访问|无法访问|购买") { return "access_unavailable" }
  if ($joined -match "Access provided by|Open PDF|institutional subscription|View PDF|Full text access|PDF loaded") { return "pdf_or_authorized" }
  if ($joined -match "Access through your organization|Organization name|Find your institution|institution login|institutional login|OpenAthens|Shibboleth") { return "auth_required" }
  if ((($Title + "`n" + $Url) -match "(?i)sign\s*in|log\s*in|login|signin|sign-in|openathens|shibboleth") -and $joined -notmatch "MDPI Open Access Journals") { return "auth_required" }
  return "unknown_visible"
}

$profileFilter = Normalize-PathForCompare -PathValue $BrowserProfile

$processes = Get-CimInstance Win32_Process | Where-Object {
  ($_.ExecutablePath -like '*instsci*_browsers*cloakbrowser*chrome.exe') -or
  ($_.CommandLine -match 'instsci[\\/]+_browsers[\\/]+cloakbrowser')
}

$windowRows = @()
$chromeProcesses = @($processes | Sort-Object ProcessId)
$mainWindows = @()

foreach ($proc in $chromeProcesses) {
  $gp = Get-Process -Id $proc.ProcessId -ErrorAction SilentlyContinue
  if (-not $gp -or $gp.MainWindowHandle -eq 0) { continue }
  $mainWindows += [pscustomobject]@{
    Process = $proc
    GetProcess = $gp
  }
}

$index = 0
foreach ($entry in $mainWindows) {
  $index += 1
  $proc = $entry.Process
  $gp = $entry.GetProcess
  $profileDir = Get-CommandLineProfile -CommandLine ([string]$proc.CommandLine)
  $profileMatch = $true
  if (-not [string]::IsNullOrWhiteSpace($profileFilter)) {
    $profileMatch = ((Normalize-PathForCompare -PathValue $profileDir) -eq $profileFilter)
  }
  $window = Get-WindowElement -Handle $gp.MainWindowHandle
  $url = Get-AddressBarValue -Window $window
  $texts = Get-InterestingText -Window $window
  $screenshot = ""
  if ($CaptureScreenshots -ne 0) {
    $screenshot = Join-Path $OutputDir ("window_{0}_{1}.png" -f $index, $proc.ProcessId)
    $screenshotSaved = Save-WindowScreenshot -Window $window -Handle ([IntPtr]$gp.MainWindowHandle) -Path $screenshot -FocusWindow ($FocusWindows -ne 0)
    if (-not $screenshotSaved) { $screenshot = "" }
  }

  $windowRows += [pscustomobject]@{
    index = $index
    pid = $proc.ProcessId
    title = [string]$gp.MainWindowTitle
    url = $url
    profile_dir = $profileDir
    profile_match = $profileMatch
    screenshot = $screenshot
    interesting_text = $texts
  }
}

$overallState = "no_window"
$stateRows = @($windowRows | Where-Object { $_.profile_match })
if ($windowRows.Count -gt 0 -and $stateRows.Count -eq 0) {
  $overallState = "other_windows_present"
} elseif ($stateRows.Count -gt 0) {
  $states = @()
  foreach ($row in $stateRows) {
    $states += Classify-State -Title $row.title -Url $row.url -Texts $row.interesting_text -WindowCount $stateRows.Count
  }
  if ($states -contains "publisher_error") { $overallState = "publisher_error" }
  elseif ($states -contains "waf_blocked") { $overallState = "waf_blocked" }
  elseif ($states -contains "human_verification_required") { $overallState = "human_verification_required" }
  elseif ($states -contains "access_unavailable") { $overallState = "access_unavailable" }
  elseif ($states -contains "auth_required") { $overallState = "auth_required" }
  elseif ($states -contains "pdf_or_authorized") { $overallState = "pdf_or_authorized" }
  elseif (($states | Where-Object { $_ -eq "blank" }).Count -ge 1) { $overallState = "blank" }
  else { $overallState = "unknown_visible" }
}

$recommendation = switch ($overallState) {
  "no_window" { "No CloakBrowser window found. Safe to start a new single-publisher run." }
  "blank" { "Stop before continuing. about:blank usually means startup/profile contention or an uninitialized browser." }
  "publisher_error" { "Record screenshot and diagnostic. Do not mark as unsupported; retry with another DOI or later." }
  "waf_blocked" { "Publisher WAF or Cloudflare blocker is active. Stop batch runs and retry later or use a manual browser route." }
  "human_verification_required" { "User must complete the visible human verification promptly. Rerun browser-doctor before continuing." }
  "access_unavailable" { "Visible page suggests the article is not entitled through this route. Check access in a regular browser before retrying InstSci." }
  "auth_required" { "User must complete institution login/selection. Do not enter credentials automatically." }
  "pdf_or_authorized" { "Page appears authorized or PDF-related. Continue capture, then verify manifest/PDF text." }
  "other_windows_present" { "CloakBrowser windows exist, but none match the requested profile. Inspect broker/profile routing before judging this run." }
  default { "Visible state is unknown. Inspect screenshot before continuing." }
}

$report = [pscustomobject]@{
  generated_at = (Get-Date).ToString("o")
  publisher = $Publisher
  output_dir = $OutputDir
  state = $overallState
  standard_status = switch ($overallState) {
    "auth_required" { "auth_required" }
    "human_verification_required" { "human_verification_required" }
    "waf_blocked" { "waf_blocked" }
    "access_unavailable" { "access_unavailable" }
    "publisher_error" { "publisher_error" }
    default { "" }
  }
  recommendation = $recommendation
  cloakbrowser_process_count = $chromeProcesses.Count
  window_count = $windowRows.Count
  matching_window_count = $stateRows.Count
  profile_filter = $BrowserProfile
  windows = $windowRows
  capture_screenshots = ($CaptureScreenshots -ne 0)
  focus_windows = ($FocusWindows -ne 0)
}

$reportPath = Join-Path $OutputDir "inspection.json"
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportPath -Encoding UTF8
$report | ConvertTo-Json -Depth 8
'''

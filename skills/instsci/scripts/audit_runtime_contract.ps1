param(
  [string]$Python = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Python)) {
  $cmd = Get-Command instsci -ErrorAction SilentlyContinue
  if (-not $cmd) {
    throw "instsci command is not on PATH. Pass -Python <path-to-installs-python>."
  }
  $launcher = [System.IO.File]::ReadAllBytes($cmd.Path)
  $strings = [System.Text.Encoding]::ASCII.GetString($launcher)
  if ($strings -match '#!([^\r\n]+python\.exe)') {
    $Python = $Matches[1]
  }
}

if ([string]::IsNullOrWhiteSpace($Python) -or -not (Test-Path -LiteralPath $Python)) {
  throw "Could not locate InstSci runtime Python. Pass -Python <path-to-installs-python>."
}

$script = @'
import importlib.util
import pathlib
import sys

mods = ["instsci.cli", "instsci.browser_doctor", "instsci.publisher_matrix", "instsci.publisher_batch"]
paths = []
for name in mods:
    spec = importlib.util.find_spec(name)
    if not spec or not spec.origin:
        raise SystemExit(f"missing module: {name}")
    paths.append(spec.origin)
for path in paths:
    source = pathlib.Path(path).read_text(encoding="utf-8")
    compile(source, path, "exec")

from instsci.publisher_matrix import build_publisher_matrix_report

unknown = build_publisher_matrix_report("runtime-contract-unknown")
if unknown["items"][0]["batch_recommendation"] == "batch_ok":
    raise SystemExit("unknown publisher failed open")

print("runtime contract OK")
for path in paths:
    print(pathlib.Path(path))
'@

$script | & $Python -
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

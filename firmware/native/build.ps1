<#
.SYNOPSIS
  Build the TactiDose native conformance harness (firmware/native/bin/harness, static Linux ELF) in Docker.

.DESCRIPTION
  Runs firmware/native/build.sh inside the gcc image (default gcc:14). The repository path may
  contain spaces. Afterwards run the conformance suite against the firmware core:

    python -m tactidose.hardware.conformance --target native

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File firmware\native\build.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File firmware\native\build.ps1 -Image gcc:13
#>
param(
    [string]$Image = $(if ($env:TACTIDOSE_GCC_IMAGE) { $env:TACTIDOSE_GCC_IMAGE } else { "gcc:14" })
)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is not installed or not on PATH (Docker Desktop with Linux containers is required)."
}
docker run --rm --network none -v "${repo}:/work" -w /work $Image sh /work/firmware/native/build.sh --in-container
if ($LASTEXITCODE -ne 0) { throw "native harness build failed (exit code $LASTEXITCODE)" }

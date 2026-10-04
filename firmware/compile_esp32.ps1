<#
.SYNOPSIS
  Compile-check the TactiDose reference firmware for a real ESP32 (arduino-cli in Docker).

.DESCRIPTION
  Runs firmware/compile_esp32.sh inside a python:3.12 container. Builds six variants with all
  warnings enabled: MECHANISM_CAROUSEL with STEP/DIR and with ULN2003, and
  MECHANISM_PER_CONTAINER_SERVO, each with the shipped config.h and with an alternate configuration
  (every preprocessor branch, drop sensor on and off). Cores, tools, libraries, arduino-cli and the
  build cache live in the Docker volume "tactidose-arduino": the first run downloads ~1 GB, later
  runs only compile and need no network. -Update refreshes the cached core and libraries.

  Behind a TLS-inspecting corporate proxy (Zscaler etc.), downloads fail with "unable to get local
  issuer certificate". Pass -CaSubject Zscaler to export the matching root certificate (public)
  from the Windows certificate store and trust it inside the container, or -CaCert <pem file>.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File firmware\compile_esp32.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File firmware\compile_esp32.ps1 -CaSubject Zscaler -CoreVersion 3.3.12
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File firmware\compile_esp32.ps1 -CaSubject Zscaler -Update
#>
param(
    [string]$Fqbn = $(if ($env:FQBN) { $env:FQBN } else { "esp32:esp32:esp32" }),
    [string]$CoreVersion = $env:ESP32_CORE_VERSION,
    [string]$CaCert = $env:TACTIDOSE_EXTRA_CA_CERT,
    [string]$CaSubject = "",
    [switch]$Update,
    [string]$Image = $(if ($env:TACTIDOSE_ARDUINO_IMAGE) { $env:TACTIDOSE_ARDUINO_IMAGE } else { "python:3.12" }),
    [string]$Volume = $(if ($env:TACTIDOSE_ARDUINO_VOLUME) { $env:TACTIDOSE_ARDUINO_VOLUME } else { "tactidose-arduino" })
)
$ErrorActionPreference = "Stop"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is not installed or not on PATH (Docker Desktop with Linux containers is required)."
}
$fw = (Resolve-Path $PSScriptRoot).Path
$dockerArgs = @("run", "--rm", "-v", "${Volume}:/arduino", "-v", "${fw}:/fw:ro",
    "-e", "FQBN=$Fqbn", "-e", "ESP32_CORE_VERSION=$CoreVersion",
    "-e", "TACTIDOSE_ARDUINO_UPDATE=$(if ($Update) { '1' } else { '0' })")

if ($CaSubject) {
    $certs = @(Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root |
        Where-Object { $_.Subject -like "*$CaSubject*" } | Sort-Object Thumbprint -Unique)
    if ($certs.Count -eq 0) { throw "no root certificate matching '$CaSubject' in the Windows certificate store" }
    $CaCert = Join-Path ([System.IO.Path]::GetTempPath()) "tactidose-extra-ca.pem"
    $pem = ($certs | ForEach-Object {
            "-----BEGIN CERTIFICATE-----`n" +
            [Convert]::ToBase64String($_.RawData, 'InsertLineBreaks').Replace("`r`n", "`n") +
            "`n-----END CERTIFICATE-----`n"
        }) -join ""
    [System.IO.File]::WriteAllText($CaCert, $pem)
    Write-Host "trusting $($certs.Count) root certificate(s) matching '$CaSubject' inside the container"
}
if ($CaCert) {
    $dockerArgs += @("-v", "$((Resolve-Path $CaCert).Path):/ca/extra-ca.pem:ro")
}
$dockerArgs += @($Image, "bash", "/fw/compile_esp32.sh", "--in-container")
& docker @dockerArgs
if ($LASTEXITCODE -ne 0) { throw "ESP32 compile failed (exit code $LASTEXITCODE)" }

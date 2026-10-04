#!/usr/bin/env bash
# Compile-check the TactiDose reference firmware for a real ESP32 with arduino-cli, inside Docker.
# Builds four variants with all warnings enabled: DRIVER_STEP_DIR and DRIVER_ULN2003 with the
# shipped config.h, plus one alternate configuration of each (no EN pin, no cancel button, LED,
# DIR_INVERT, no home sensor) so that every preprocessor branch is compiled.
#
#   bash firmware/compile_esp32.sh           Linux / macOS / WSL / Git Bash
#   firmware\compile_esp32.ps1               Windows PowerShell (can export a corporate root CA)
#
# Environment (all optional):
#   FQBN                      board, default esp32:esp32:esp32 ("ESP32 Dev Module")
#   ESP32_CORE_VERSION        pin the esp32 core, e.g. 3.3.12 (default: latest)
#   TACTIDOSE_EXTRA_CA_CERT   PEM file with an extra root CA, needed behind a TLS-inspecting proxy
#                             (symptom: "SSL certificate problem: unable to get local issuer certificate")
#   TACTIDOSE_ARDUINO_IMAGE   Docker image, default python:3.12 (needs bash, curl, python3)
#   TACTIDOSE_ARDUINO_VOLUME  cache volume, default tactidose-arduino
#
# The first run downloads arduino-cli, the ESP32 core and toolchains (~1 GB) and the AccelStepper +
# ESP32Servo libraries into the Docker volume; later runs only compile.
set -euo pipefail

if [ "${1:-}" != "--in-container" ]; then
  here=$(cd "$(dirname "$0")" && pwd)
  image="${TACTIDOSE_ARDUINO_IMAGE:-python:3.12}"
  volume="${TACTIDOSE_ARDUINO_VOLUME:-tactidose-arduino}"
  windows=0
  case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) windows=1 ;; esac
  src="$here"
  if [ "$windows" = 1 ]; then
    src=$(cd "$here" && pwd -W)
    export MSYS_NO_PATHCONV=1 # keep "/fw" etc. as they are
  fi
  args=(run --rm -v "$volume:/arduino" -v "$src:/fw:ro" -e FQBN -e ESP32_CORE_VERSION)
  if [ -n "${TACTIDOSE_EXTRA_CA_CERT:-}" ]; then
    ca="$TACTIDOSE_EXTRA_CA_CERT"
    [ "$windows" = 1 ] && ca="$(cd "$(dirname "$ca")" && pwd -W)/$(basename "$ca")"
    args+=(-v "$ca:/ca/extra-ca.pem:ro")
  fi
  exec docker "${args[@]}" "$image" bash /fw/compile_esp32.sh --in-container
fi

# ------------------------------------------------------------------ inside the container
FQBN="${FQBN:-esp32:esp32:esp32}"
if [ -f /ca/extra-ca.pem ]; then
  cp /ca/extra-ca.pem /usr/local/share/ca-certificates/tactidose-extra-ca.crt
  update-ca-certificates >/dev/null 2>&1
fi
export ARDUINO_DIRECTORIES_DATA=/arduino/data
export ARDUINO_DIRECTORIES_DOWNLOADS=/arduino/downloads
export ARDUINO_DIRECTORIES_USER=/arduino/user
export ARDUINO_BOARD_MANAGER_ADDITIONAL_URLS=https://espressif.github.io/arduino-esp32/package_esp32_index.json
cli=/arduino/bin/arduino-cli
if [ ! -x "$cli" ]; then
  mkdir -p /arduino/bin
  curl -fsSL https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Linux_64bit.tar.gz |
    tar -xz -C /arduino/bin arduino-cli
fi
"$cli" version
"$cli" core update-index
"$cli" core install "esp32:esp32${ESP32_CORE_VERSION:+@$ESP32_CORE_VERSION}"
"$cli" lib update-index
"$cli" lib install AccelStepper ESP32Servo
"$cli" core list
"$cli" lib list

# name | extra compiler flags | config.h overrides (KEY=VALUE, applied to a copy). The *_ALT
# variants compile the preprocessor branches the defaults skip.
variants=(
  "STEP_DIR||"
  "ULN2003|-DDRIVER_TYPE=2|"
  "STEP_DIR_ALT||PIN_ENABLE=-1 PIN_CANCEL_BUTTON=-1 PIN_STATUS_LED=2 ENABLE_ACTIVE_LOW=0"
  "ULN2003_ALT|-DDRIVER_TYPE=2|DIR_INVERT=1 HAS_HOME_SENSOR=0 PIN_STATUS_LED=2"
)
status=0
summary=()
for spec in "${variants[@]}"; do
  IFS='|' read -r name flags edits <<<"$spec"
  sketch="/tmp/sketch-$name/tactidose_esp32"
  rm -rf "/tmp/sketch-$name"
  mkdir -p "/tmp/sketch-$name"
  cp -r /fw/tactidose_esp32 "/tmp/sketch-$name/"
  for edit in $edits; do
    sed -i -E "s/^#define ${edit%%=*} .*/#define ${edit%%=*} ${edit#*=}/" "$sketch/config.h"
  done
  props=()
  [ -n "$flags" ] && props=(--build-property "compiler.cpp.extra_flags=$flags")
  echo "=== $FQBN, variant $name ${flags} ${edits} ==="
  log="/tmp/compile-$name.log"
  result=ok
  if ! "$cli" compile --fqbn "$FQBN" --warnings all --build-path "/tmp/build-$name" "${props[@]}" "$sketch" \
      2>&1 | tee "$log"; then
    status=1
    result=FAILED
  fi
  # Warnings that point into the sketch (our code), as opposed to the core or the libraries.
  ours=$(grep -E "(/sketch|tactidose_esp32)/[^ :]*:[0-9]+(:[0-9]+)?: warning:" "$log" || true)
  count=$(printf '%s' "$ours" | grep -c . || true)
  [ -n "$ours" ] && printf '%s\n' "$ours"
  summary+=("$name: $result, $count warning(s) in sketch files")
done
echo "=== summary ==="
printf '%s\n' "${summary[@]}"
exit "$status"

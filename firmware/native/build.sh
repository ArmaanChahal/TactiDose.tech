#!/bin/sh
# Build the TactiDose native conformance harness: firmware/native/bin/harness (static Linux ELF).
#
#   sh firmware/native/build.sh            local g++ on Linux if available, otherwise Docker (gcc:14)
#   sh firmware/native/build.sh --docker   always build inside Docker
#   sh firmware/native/build.sh --local    always use the local compiler ($CXX, default g++)
#   Windows PowerShell: firmware/native/build.ps1   |   any OS: python -m tactidose.hardware.conformance_native build
#
# Also checks that the firmware core stays portable C++11 (Arduino-ESP32 2.x compiles gnu++11)
# and runs the config.h static_asserts for both mechanisms and both driver types. Warnings are errors.
set -eu

IMAGE="${TACTIDOSE_GCC_IMAGE:-gcc:14}"
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

mode=auto
case "${1:-}" in
  "") ;;
  --docker) mode=docker ;;
  --local|--in-container) mode=local ;;
  *) echo "usage: $0 [--docker|--local]" >&2; exit 2 ;;
esac
if [ "$mode" = auto ]; then
  if [ "$(uname -s)" = Linux ] && command -v "${CXX:-g++}" >/dev/null 2>&1; then mode=local; else mode=docker; fi
fi

if [ "$mode" = docker ]; then
  src="$REPO"
  case "$(uname -s)" in
    # Git Bash / MSYS: give Docker a Windows path and stop MSYS from rewriting "/work" arguments.
    MINGW*|MSYS*|CYGWIN*) src=$(cd "$REPO" && pwd -W); MSYS_NO_PATHCONV=1; export MSYS_NO_PATHCONV ;;
  esac
  exec docker run --rm --network none -v "$src:/work" -w /work "$IMAGE" sh /work/firmware/native/build.sh --in-container
fi

cd "$REPO"
CXX="${CXX:-g++}"
CORE=firmware/tactidose_esp32
NATIVE=firmware/native
WARN="-Wall -Wextra -Werror -Wpedantic -Wshadow -Wconversion -Wsign-conversion -Wdouble-promotion -Wformat=2"
STATIC=""
[ "$(uname -s)" = Linux ] && STATIC="-static"

echo "== core is portable C++11"
$CXX -std=c++11 $WARN -fsyntax-only -I"$CORE" "$CORE/TactiDoseCore.cpp"
echo "== config.h static_asserts (carousel STEP/DIR and ULN2003, per-container servo)"
$CXX -std=c++11 $WARN -fsyntax-only -I"$CORE" "$NATIVE/config_check.cpp"
$CXX -std=c++11 $WARN -fsyntax-only -DDRIVER_TYPE=2 -I"$CORE" "$NATIVE/config_check.cpp"
$CXX -std=c++11 $WARN -fsyntax-only -DMECHANISM=2 -I"$CORE" "$NATIVE/config_check.cpp"
echo "== harness (C++17)"
mkdir -p "$NATIVE/bin"
# A unique temporary name: several test sessions may rebuild at the same time.
tmp=$(mktemp "$NATIVE/bin/harness.XXXXXX")
trap 'rm -f "$tmp"' EXIT
$CXX -std=c++17 -O2 $WARN $STATIC -I"$CORE" -I"$NATIVE" -o "$tmp" \
  "$NATIVE/harness.cpp" "$NATIVE/FakeHal.cpp" "$CORE/TactiDoseCore.cpp"
chmod 755 "$tmp"
mv -f "$tmp" "$NATIVE/bin/harness"
echo "built $NATIVE/bin/harness with $($CXX --version | head -n 1)"

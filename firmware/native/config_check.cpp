/*
 * config_check.cpp -- compiles config.h + ConfigCheck.h natively so their static_asserts run
 * without the ESP32 toolchain. build.sh compiles it (syntax only) for both driver types.
 */
#include "config.h"
#include "ConfigCheck.h"

int main() { return tactidose::makeCoreConfig().numSlots == NUM_SLOTS ? 0 : 1; }

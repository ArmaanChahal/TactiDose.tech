/*
 * harness.cpp -- native conformance harness for the TactiDose firmware core.
 *
 * Runs firmware/tactidose_esp32/TactiDoseCore.cpp (the exact code that runs on the ESP32) against
 * FakeHal (simulated carousel, simulated time) and speaks the stdin/stdout protocol of
 * docs/ARCHITECTURE.md §7. Driven by tactidose/hardware/conformance_native.py (NativeTarget).
 *
 * stdin, one directive per line; stdout: firmware serial lines verbatim (without \r), then exactly
 * one "!ack <sim_time_ms>" per stdin line:
 *   > <text>                deliver <text> as one serial line (">" alone = empty line)
 *   !reset                  fresh device: carousel 1600 steps before home, sensor ok, jam off,
 *                           buttons released, firmware not booted, sim time 0
 *   !boot ok|dead|none      (re)initialise the firmware (setup()); keeps the physical position
 *   !tick <ms>              advance simulated time, calling loop() every 1 ms
 *   !button CONFIRM|CANCEL 1|0
 *   !sensor ok|dead         (extension: stuck = always active)
 *   !jam 1|0
 *   !quit
 * Extensions (not used by the frozen runner; they never change the simulated state):
 *   !peek <max_ms>          "!peek <n>": the next n ms are guaranteed silent (no serial line)
 *                           if no input arrives -- computed on a snapshot that is then restored.
 *                           Lets NativeTarget skip silent ticks exactly (see conformance_native.py).
 *   !physical               "!physical key=value ...": physics + safety-oracle report
 *   !parsehex <hex> [slots] "!parse ...": result of the core's parseLine() for those bytes
 *                           ("-" = zero bytes)
 * Extensions that do change state:
 *   !rx <hex>               deliver raw bytes (no implicit newline), e.g. "\r" terminators
 *   !set <key>=<value>      firmware setting for the following !boot (keys of --set, or millisOffset)
 *   !defaults               back to the command-line settings
 * A line that cannot be processed produces "!err <reason>" before its !ack.
 *
 * Command line: harness [--millis-offset N] [--set key=value]...
 */
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <iostream>
#include <list>
#include <string>
#include <vector>

#include "FakeHal.h"
#include "TactiDoseCore.h"

namespace {

const uint64_t kMaxTickMs = 100000000ULL;

std::vector<std::string> splitWords(const std::string& s) {
  std::vector<std::string> words;
  size_t i = 0;
  while (i < s.size()) {
    while (i < s.size() && (s[i] == ' ' || s[i] == '\t')) ++i;
    const size_t start = i;
    while (i < s.size() && s[i] != ' ' && s[i] != '\t') ++i;
    if (i > start) words.push_back(s.substr(start, i - start));
  }
  return words;
}

std::string upper(std::string s) {
  for (char& c : s) {
    if (c >= 'a' && c <= 'z') c = static_cast<char>(c - ('a' - 'A'));
  }
  return s;
}

bool parseUnsigned(const std::string& text, uint64_t maxValue, uint64_t* out) {
  if (text.empty() || text.size() > 19) return false;
  uint64_t value = 0;
  for (char c : text) {
    if (c < '0' || c > '9') return false;
    value = value * 10U + static_cast<uint64_t>(c - '0');
  }
  if (value > maxValue) return false;
  *out = value;
  return true;
}

bool parseLong(const std::string& text, long* out) {
  if (text.empty()) return false;
  errno = 0;
  char* end = nullptr;
  const long value = strtol(text.c_str(), &end, 10);
  if (errno != 0 || end == nullptr || *end != '\0') return false;
  *out = value;
  return true;
}

bool parseBool(const std::string& text, bool* out) {
  if (text == "1" || text == "true" || text == "on") {
    *out = true;
    return true;
  }
  if (text == "0" || text == "false" || text == "off") {
    *out = false;
    return true;
  }
  return false;
}

/* Hex digits -> bytes; "-" stands for zero bytes. */
bool decodeHex(const std::string& hex, std::string* out) {
  out->clear();
  if (hex == "-") return true;
  if (hex.size() % 2 != 0) return false;
  for (size_t i = 0; i < hex.size(); i += 2) {
    int value = 0;
    for (size_t k = 0; k < 2; ++k) {
      const char c = hex[i + k];
      int nibble;
      if (c >= '0' && c <= '9') {
        nibble = c - '0';
      } else if (c >= 'a' && c <= 'f') {
        nibble = c - 'a' + 10;
      } else if (c >= 'A' && c <= 'F') {
        nibble = c - 'A' + 10;
      } else {
        return false;
      }
      value = value * 16 + nibble;
    }
    out->push_back(static_cast<char>(value));
  }
  return true;
}

/* Firmware configuration matching the conformance.json "harness" physics and the Python
 * simulator defaults (docs/ARCHITECTURE.md SimConfig). Independent of config.h on purpose. */
tactidose::CoreConfig harnessConfig() {
  tactidose::CoreConfig c;
  c.fwVersion = "1.0.0-native";
  c.numSlots = 6;
  c.stepsPerRev = 3200.0f;
  c.maxSpeed = 1600.0f;
  c.acceleration = 3200.0f;
  c.hasHomeSensor = true;
  c.autoHomeOnBoot = true;
  c.homingDir = 1;
  c.homingSpeed = 400.0f;
  c.homingSlowSpeed = 100.0f;
  c.homeMaxTravelRevs = 1.25f;
  c.homeTimeoutMs = 20000;
  c.homeBackoffSteps = 100;
  c.homeReleaseMaxRevs = 0.25f;
  c.homeOffsetSteps = 0;
  c.homeDebounceMs = 10;
  c.verifySlotWithHomeSensor = true; /* slot 0 lies inside the simulated sensor zone */
  c.settleMs = 300;
  c.gateTravelMs = 400;
  c.servoClosedDeg = 20;
  c.servoOpenDeg = 90;
  c.gateMaxOpenMs = 120000;
  c.debounceMs = 30;
  c.motionTimeoutFactor = 2.0f;
  c.motionTimeoutMarginMs = 2000;
  c.holdWhenIdle = true;
  c.debugLog = true;
  return c;
}

/* Stable storage for strings referenced by CoreConfig::fwVersion (never freed). */
const char* internString(const std::string& s) {
  static std::list<std::string> pool;
  pool.push_back(s);
  return pool.back().c_str();
}

/* --set / !set key=value: tweak the firmware configuration (tests cover config variants). */
bool applySetting(const std::string& assignment, tactidose::CoreConfig* c) {
  const size_t eq = assignment.find('=');
  if (eq == std::string::npos) return false;
  const std::string key = assignment.substr(0, eq);
  const std::string value = assignment.substr(eq + 1);
  if (key == "fw") {
    if (value.empty() || value.find(' ') != std::string::npos) return false;
    c->fwVersion = internString(value);
    return true;
  }
  bool* flag = nullptr;
  if (key == "verifySlot") flag = &c->verifySlotWithHomeSensor;
  if (key == "debugLog") flag = &c->debugLog;
  if (key == "holdWhenIdle") flag = &c->holdWhenIdle;
  if (key == "autoHome") flag = &c->autoHomeOnBoot;
  if (flag != nullptr) return parseBool(value, flag);
  long n = 0;
  if (!parseLong(value, &n)) return false;
  if (key == "homeOffsetSteps") {
    c->homeOffsetSteps = n;
    return true;
  }
  if (n < 0) return false;
  if (key == "homeBackoffSteps") {
    c->homeBackoffSteps = n;
  } else if (key == "homeDebounceMs" && n <= 1000) {
    c->homeDebounceMs = static_cast<uint16_t>(n);
  } else if (key == "settleMs" && n <= 60000) {
    c->settleMs = static_cast<uint16_t>(n);
  } else if (key == "debounceMs" && n <= 1000) {
    c->debounceMs = static_cast<uint16_t>(n);
  } else if (key == "homeTimeoutMs" && n > 0) {
    c->homeTimeoutMs = static_cast<uint32_t>(n);
  } else if (key == "gateMaxOpenMs" && n > 0) {
    c->gateMaxOpenMs = static_cast<uint32_t>(n);
  } else if (key == "homingSpeed" && n > 0) {
    c->homingSpeed = static_cast<float>(n);
  } else if (key == "homingSlowSpeed" && n > 0) {
    c->homingSlowSpeed = static_cast<float>(n);
  } else if (key == "maxSpeed" && n > 0) {
    c->maxSpeed = static_cast<float>(n);
  } else if (key == "acceleration" && n > 0) {
    c->acceleration = static_cast<float>(n);
  } else {
    return false;
  }
  return true;
}

class Harness {
 public:
  Harness(const tactidose::CoreConfig& config, const harness::Physics& physics, uint32_t millisOffset)
      : hal_(physics),
        config_(config),
        core_(hal_, config_),
        defaults_(config),
        millisOffset_(millisOffset),
        defaultMillisOffset_(millisOffset) {
    hal_.setOutput(&out_);
    hal_.setMillisOffset(millisOffset);
  }

  /* Process one stdin line; false after !quit. */
  bool handle(const std::string& line) {
    bool keepGoing = true;
    if (!line.empty() && line[0] == '>') {
      std::string text = line.substr(1);
      if (!text.empty() && text[0] == ' ') text.erase(0, 1);
      text.push_back('\n');
      deliver(text);
    } else if (!line.empty() && line[0] == '!') {
      keepGoing = directive(splitWords(line.substr(1)));
    } else {
      error("expected '> <text>' or a '!' directive");
    }
    out_ += "!ack " + std::to_string(hal_.simMs()) + "\n";
    return keepGoing;
  }

  std::string& output() { return out_; }

 private:
  void error(const std::string& reason) { out_ += "!err " + reason + "\n"; }

  /* Serial bytes arrive; a real loop() runs within microseconds, so pump it once at the current
   * time (no time passes: stepper/timers are idempotent at an unchanged millis()). */
  void deliver(const std::string& bytes) {
    if (!hal_.queueRx(bytes.data(), bytes.size())) error("rx buffer overflow (bytes dropped)");
    if (booted_) core_.loop();
  }

  void tick(uint64_t ms) {
    for (uint64_t i = 0; i < ms; ++i) {
      hal_.advanceOneMs();
      if (booted_) core_.loop();
    }
  }

  uint64_t peekSilentMs(uint64_t maxMs) {
    if (!booted_) return maxMs;
    const harness::FakeHal savedHal = hal_;
    const tactidose::TactiDoseCore savedCore = core_;
    hal_.setMuted(true);
    uint64_t silent = maxMs;
    for (uint64_t i = 0; i < maxMs; ++i) {
      hal_.advanceOneMs();
      core_.loop();
      if (hal_.mutedLines() > 0) {
        silent = i;
        break;
      }
    }
    hal_ = savedHal;
    core_ = savedCore;
    return silent;
  }

  bool directive(const std::vector<std::string>& w) {
    const std::string cmd = w.empty() ? std::string() : w[0];
    const size_t argc = w.size() - (w.empty() ? 0 : 1);
    if (cmd == "reset" && argc == 0) {
      hal_.resetAll();
      booted_ = false;
    } else if (cmd == "boot" && argc == 1 && (w[1] == "ok" || w[1] == "dead" || w[1] == "none")) {
      const bool fitted = w[1] != "none";
      if (w[1] == "ok") hal_.setSensor(harness::SensorMode::kOk);
      if (w[1] == "dead") hal_.setSensor(harness::SensorMode::kDead);
      hal_.setMillisOffset(millisOffset_);
      hal_.powerOn(fitted);
      config_.hasHomeSensor = fitted;
      core_ = tactidose::TactiDoseCore(hal_, config_);
      core_.begin();
      booted_ = true;
    } else if (cmd == "tick" && argc == 1) {
      uint64_t ms = 0;
      if (!parseUnsigned(w[1], kMaxTickMs, &ms)) {
        error("tick: expected 0.." + std::to_string(kMaxTickMs) + " ms");
      } else {
        tick(ms);
      }
    } else if (cmd == "button" && argc == 2 && (w[2] == "1" || w[2] == "0") &&
               (upper(w[1]) == "CONFIRM" || upper(w[1]) == "CANCEL")) {
      hal_.setButton(upper(w[1]) == "CONFIRM" ? tactidose::Button::kConfirm : tactidose::Button::kCancel,
                     w[2] == "1");
    } else if (cmd == "sensor" && argc == 1 && (w[1] == "ok" || w[1] == "dead" || w[1] == "stuck")) {
      hal_.setSensor(w[1] == "ok" ? harness::SensorMode::kOk
                                  : (w[1] == "dead" ? harness::SensorMode::kDead : harness::SensorMode::kStuck));
    } else if (cmd == "jam" && argc == 1 && (w[1] == "1" || w[1] == "0")) {
      hal_.setJam(w[1] == "1");
    } else if (cmd == "quit" && argc == 0) {
      return false;
    } else if (cmd == "peek" && argc == 1) {
      uint64_t maxMs = 0;
      if (!parseUnsigned(w[1], kMaxTickMs, &maxMs)) {
        error("peek: expected 0.." + std::to_string(kMaxTickMs) + " ms");
      } else {
        out_ += "!peek " + std::to_string(peekSilentMs(maxMs)) + "\n";
      }
    } else if (cmd == "physical" && argc == 0) {
      out_ += "!physical ";
      hal_.describe(out_);
      out_ += std::string(" fw_state=") + tactidose::stateName(core_.state()) + " fw_homed=" +
              (core_.homed() ? "1" : "0") + " fw_slot=" + std::to_string(core_.slot()) +
              " booted=" + (booted_ ? "1" : "0") + "\n";
    } else if (cmd == "set" && argc == 1) {
      set(w[1]);
    } else if (cmd == "defaults" && argc == 0) {
      config_ = defaults_;
      millisOffset_ = defaultMillisOffset_;
    } else if (cmd == "parsehex" && (argc == 1 || argc == 2)) {
      parseHex(w);
    } else if (cmd == "rx" && argc == 1) {
      std::string bytes;
      if (!decodeHex(w[1], &bytes)) {
        error("rx: expected an even number of hex digits");
      } else {
        deliver(bytes);
      }
    } else {
      error("unknown or malformed directive: !" + cmd);
    }
    return true;
  }

  void set(const std::string& assignment) {
    const std::string prefix = "millisOffset=";
    if (assignment.compare(0, prefix.size(), prefix) == 0) {
      uint64_t value = 0;
      if (!parseUnsigned(assignment.substr(prefix.size()), 0xFFFFFFFFULL, &value)) {
        error("set: millisOffset must be 0..4294967295");
      } else {
        millisOffset_ = static_cast<uint32_t>(value);
      }
    } else if (!applySetting(assignment, &config_)) {
      error("set: unknown key or bad value: " + assignment);
    }
  }

  void parseHex(const std::vector<std::string>& w) {
    std::string bytes;
    uint64_t slots = config_.numSlots;
    if (!decodeHex(w[1], &bytes) || (w.size() == 3 && !parseUnsigned(w[2], 255, &slots))) {
      error("parsehex: expected <hex> [num_slots]");
      return;
    }
    const tactidose::ParsedLine p =
        tactidose::parseLine(bytes.data(), bytes.size(), static_cast<uint8_t>(slots));
    if (p.empty) {
      out_ += "!parse empty\n";
    } else if (p.error == tactidose::ErrorCode::kNone) {
      out_ += std::string("!parse ok ") + tactidose::commandName(p.command) +
              (p.slot >= 0 ? " " + std::to_string(p.slot) : std::string(" -")) + "\n";
    } else {
      out_ += std::string("!parse err ") + tactidose::errorName(p.error) + " " +
              (p.command == tactidose::Command::kNone ? "-" : tactidose::commandName(p.command)) + "\n";
    }
  }

  harness::FakeHal hal_;
  tactidose::CoreConfig config_; /* used by the next !boot */
  tactidose::TactiDoseCore core_;
  const tactidose::CoreConfig defaults_;
  uint32_t millisOffset_;
  const uint32_t defaultMillisOffset_;
  bool booted_ = false;
  std::string out_;
};

int usage(const char* argv0) {
  fprintf(stderr,
          "usage: %s [--millis-offset N] [--set key=value]...\n"
          "  speaks the docs/ARCHITECTURE.md section 7 protocol on stdin/stdout (see harness.cpp)\n",
          argv0);
  return 2;
}

}  // namespace

int main(int argc, char** argv) {
  tactidose::CoreConfig config = harnessConfig();
  uint32_t millisOffset = 0;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--millis-offset" && i + 1 < argc) {
      uint64_t value = 0;
      if (!parseUnsigned(argv[++i], 0xFFFFFFFFULL, &value)) return usage(argv[0]);
      millisOffset = static_cast<uint32_t>(value);
    } else if (arg == "--set" && i + 1 < argc) {
      if (!applySetting(argv[++i], &config)) {
        fprintf(stderr, "bad --set %s\n", argv[i]);
        return usage(argv[0]);
      }
    } else {
      return usage(argv[0]);
    }
  }
  harness::Physics physics;
  physics.numSlots = config.numSlots;
  physics.servoClosedDeg = config.servoClosedDeg;
  physics.servoOpenDeg = config.servoOpenDeg;
  physics.gateTravelMs = config.gateTravelMs;

  Harness harness(config, physics, millisOffset);
  std::ios::sync_with_stdio(false);
  std::string line;
  while (std::getline(std::cin, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    const bool keepGoing = harness.handle(line);
    std::string& out = harness.output();
    fwrite(out.data(), 1, out.size(), stdout);
    fflush(stdout);
    out.clear();
    if (!keepGoing) break;
  }
  return 0;
}

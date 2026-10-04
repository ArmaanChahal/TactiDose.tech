# Switching to the real buzzer

Today the guided demo uses the **laptop tone**: the kiosk or demo screen beeps. The firmware,
simulator and host already support a real buzzer on the ESP32. Switching takes one firmware
value and one setting. Protocol details: [SERIAL_PROTOCOL.md §13](SERIAL_PROTOCOL.md).

## Checklist

1. **Wire it.**
   * **Active buzzer** (has its own oscillator; usually sealed and marked "+"): connect GPIO → buzzer
     "+", buzzer "–" → GND. Above about 20 mA, drive it through an NPN transistor (base resistor
     about 1 kΩ).
   * **Passive buzzer or piezo disc:** same wiring. The firmware generates the tone.
   * Use a free output-capable GPIO, for example **33**. Do not use:
     * 6–11 (flash)
     * 34–39 (input only)
     * 1/3 (USB serial)
     * the strapping pins 0, 2, 5, 12, 15
2. **Set the pin.** In `firmware/tactidose_esp32/config.h`, block **"BUZZER (edit here)"**:
   * `BUZZER_PIN`: replace `-1` (the TODO) with your GPIO.
   * `BUZZER_TYPE`: `BUZZER_ACTIVE` or `BUZZER_PASSIVE`.
   * Only if needed: `BUZZER_ACTIVE_HIGH`, `BUZZER_TONE_HZ` (passive only), `BUZZER_MAX_ON_MS`.
3. **Flash the firmware:** `sh firmware/compile_esp32.sh` (Windows: `compile_esp32.ps1`), then upload
   as described in [firmware/README.md](../firmware/README.md). The compile-time checks reject an
   invalid pin.
4. **Flip the setting.** In `.env`, set `TACTIDOSE_BUZZER_BACKEND=serial`. Use `both` to also keep
   the laptop tone.
5. **Test the wiring without the demo:**
   ```bash
   python -m tactidose buzzer-test --serial auto      # or --serial COM5 / /dev/ttyUSB0
   ```
   The buzzer sounds for 2 seconds. The command prints what the device answered:

   | Output | Meaning |
   |---|---|
   | `OK: the device's buzzer was switched on.` | Done. If you heard nothing, check the wiring and `BUZZER_TYPE` |
   | `NO_BUZZER` | `BUZZER_PIN` is still `-1`: steps 2–3 |
   | `UNKNOWN_COMMAND` | The board runs older firmware: step 3 |
   | `not connected` | Cable or port problem: `python -m tactidose ports` |

   Without a board: `python -m tactidose buzzer-test --backend serial --sim`.
6. **Run the demo** (`python -m tactidose run --serial auto`). If the device's buzzer fails during a
   run (timeout, unplugged, old firmware), the laptop tone plays instead. The pill drop is never
   held up.

## Rolling back

* Set `TACTIDOSE_BUZZER_BACKEND=laptop` (the default), or remove the line, and restart the app.
  That's all; the firmware can stay as it is.
* To silence the buzzer entirely: `TACTIDOSE_BUZZER_BACKEND=none`.
* Firmware: `BUZZER_PIN -1` turns the device's buzzer off (`BUZZER` → `ERR NO_BUZZER`).

## Where every value lives

| Side | File | Values |
|---|---|---|
| Firmware | `firmware/tactidose_esp32/config.h`, block "BUZZER (edit here)" | `BUZZER_PIN`, `BUZZER_TYPE`, `BUZZER_ACTIVE_HIGH`, `BUZZER_TONE_HZ`, `BUZZER_MAX_ON_MS` |
| Host | `tactidose/hardware/buzzer_config.py` | `MAX_ON_MS`, `DEFAULT_ON_MS`, `DEFAULT_PATTERN`, `COMMAND_TIMEOUT_S`, `RETRIES`, `TEST_DURATION_MS` |
| Host setting | `.env` | `TACTIDOSE_BUZZER_BACKEND` = `laptop` \| `serial` \| `both` \| `none` |

Keep `BUZZER_MAX_ON_MS` (firmware) ≥ `MAX_ON_MS` (host).

> **Not yet checked on a real board:** the passive-buzzer code uses the LEDC API (`ledcAttach` on
> Arduino-ESP32 3.x, `ledcSetup`/`ledcAttachPin` on 2.x). The first ESP32 compile with
> `BUZZER_TYPE BUZZER_PASSIVE` should confirm it. The core logic is covered by the native
> conformance harness.

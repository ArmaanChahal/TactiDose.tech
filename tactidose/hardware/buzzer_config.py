"""Every host-side buzzer value in one place. Edit here; nothing else needs to change.

The firmware has its own block with the pin and its hard limit: the "BUZZER (edit here)" block in
``firmware/tactidose_esp32/config.h``. Which buzzer is used is the setting
``TACTIDOSE_BUZZER_BACKEND`` (laptop | serial | both | none). Checklist: ``docs/BUZZER.md``.
"""

from __future__ import annotations

#: Longest single "buzzer on" the host ever asks for, in milliseconds. Keep it equal to (or below)
#: BUZZER_MAX_ON_MS in config.h - the firmware cuts the buzzer off at its own limit anyway.
MAX_ON_MS = 10_000

#: How long the buzzer sounds when a caller does not say (milliseconds).
DEFAULT_ON_MS = 5_000

#: Sound pattern. "continuous" is the only pattern the firmware implements (BUZZER ON <ms> keeps
#: it on, BUZZER OFF stops it). The laptop tone in the browser always beeps (js/guided.js).
DEFAULT_PATTERN = "continuous"

#: Seconds the host waits for the device to answer one BUZZER command. Short on purpose: the
#: buzzer must never hold up a pill drop. 0.5 s is plenty on USB serial (a reply takes ~5 ms).
COMMAND_TIMEOUT_S = 0.5

#: Extra attempts after a timeout or a busy link before falling back to the laptop tone.
#: Each attempt can add up to COMMAND_TIMEOUT_S before the drop starts.
RETRIES = 1

#: How long ``python -m tactidose buzzer-test`` sounds the buzzer (milliseconds).
TEST_DURATION_MS = 2_000

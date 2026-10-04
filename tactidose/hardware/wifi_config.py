"""The Wi-Fi ESP32 dispenser: its address and HTTP endpoints, all in one place. Edit here.

Used when ``TACTIDOSE_HARDWARE_MODE=wifi`` (or ``python -m tactidose run --wifi``). The driver is
``tactidose/hardware/wifi_device.py``; every dispense still goes through the drop rules
(DropService: cooldown, double-dose guard, pill counts, history).

Endpoints of the ESP32 (2026-10-04):

    open lid        GET http://172.20.10.9/lid?state=open
    close lid       GET http://172.20.10.9/lid?state=close
    dispense pill N GET http://172.20.10.9/dispense?pill=N     (N = 1, 2, 3 = container 1, 2, 3)
"""

from __future__ import annotations

#: The ESP32's static IP address. ``TACTIDOSE_ESP32_URL`` in .env overrides it (e.g. another board).
ESP32_BASE_URL = "http://172.20.10.9"

#: HTTP method of every request (the endpoints work from a browser address bar, so GET).
METHOD = "GET"

#: Paths appended to the base URL. ``{pill}`` = container number counted from 1 (1, 2, 3).
LID_OPEN_PATH = "/lid?state=open"
LID_CLOSE_PATH = "/lid?state=close"
DISPENSE_PATH = "/dispense?pill={pill}"

#: Every dispense: open the lid first, dispense, then close the lid LID_CLOSE_AFTER_S seconds later
#: (so the patient can take the pill). If the lid does not open, nothing is dispensed.
#: False = dispense only (the lid buttons still work).
OPEN_LID_FOR_DISPENSE = True
#: Seconds the lid stays open after a dispense before it closes by itself.
LID_CLOSE_AFTER_S = 5.0

#: Reachability check: any HTTP answer from this path means the ESP32 is on the network.
HEALTH_PATH = "/"
#: Seconds between reachability checks while idle (the device shows "connected" / "offline").
HEALTH_INTERVAL_S = 5.0

#: Seconds to open a connection to the ESP32. If it cannot even connect, nothing was sent, so a
#: dispense is refused cleanly ("device not available") instead of being marked uncertain.
CONNECT_TIMEOUT_S = 3.0
#: Seconds to wait for the ESP32's answer to /dispense (it should answer when the pill has dropped).
#: No answer in time = the pill MAY have dropped: the drop is recorded as UNCERTAIN for a caregiver.
DISPENSE_TIMEOUT_S = 20.0
#: Seconds to wait for the answer to /lid.
LID_TIMEOUT_S = 10.0

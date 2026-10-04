"""The Wi-Fi ESP32 dispenser: its address and HTTP endpoints, all in one place. Edit here.

Used when ``TACTIDOSE_HARDWARE_MODE=wifi`` (or ``python -m tactidose run --wifi``). The driver is
``tactidose/hardware/wifi_device.py``.

Endpoints of the ESP32 (2026-10-04) and what uses them:

    dispense pill N GET http://172.20.10.9/dispense?pill=N   N = 1, 2, 3 = container 1, 2, 3.
                    Just drops the pill. Every dispense goes through the drop rules first
                    (DropService: cooldown, double-dose guard, pill counts, history).
    open lid        GET http://172.20.10.9/lid?state=open    Restocking only: doctor/family open the
    close lid       GET http://172.20.10.9/lid?state=close   lid, refill the containers, close it.
                    Never part of a dispense.
"""

from __future__ import annotations

#: The ESP32's static IP address. ``TACTIDOSE_ESP32_URL`` in .env overrides it (e.g. another board).
ESP32_BASE_URL = "http://192.168.4.1"

#: HTTP method of every request (the endpoints work from a browser address bar, so GET).
METHOD = "GET"

#: Paths appended to the base URL. ``{pill}`` = container number counted from 1 (1, 2, 3).
LID_OPEN_PATH = "/lid?state=open"
LID_CLOSE_PATH = "/lid?state=close"
DISPENSE_PATH = "/dispense?pill={pill}"

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
#: Seconds to wait for the answer to /lid (restocking).
LID_TIMEOUT_S = 10.0

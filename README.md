# TactiDose.tech

Voice-first, motorized medication-access prototype for blind and low-vision users.
A pre-confirmed schedule maps each medication to a carousel compartment; at the right
time the device rotates the correct compartment to a single opening, speaks what is
ready, records that the dose was taken, and refuses duplicate dispensing.

> **Hackathon prototype — not a medical device.** It does not diagnose, prescribe or
> change dosages. Demo with candy or labelled tokens only, never real medication.

**Status:** in active development. The contracts below are frozen; the module
implementations (hardware client + simulator, dose logic, voice, integrations,
reference firmware, UI) are landing next.

## Design in one line

**AI interprets, deterministic code authorizes and actuates.** Gemini reads labels,
Vosk hears commands, ElevenLabs speaks — but only deterministic schedule and safety
checks decide whether the carousel moves, and every uncertain outcome fails closed.

## Repository map

| Path | What it is |
|---|---|
| `TactiDose_Hardware_Software_Handoff.md` | Original product / engineering handoff |
| `docs/SERIAL_PROTOCOL.md` | Host ↔ ESP32 serial contract (for the hardware teammate) |
| `docs/ARCHITECTURE.md` | Modules, threading, dose lifecycle, safety rules |
| `docs/API.md` | HTTP API used by the kiosk, caregiver and demo UIs |
| `tactidose/hardware/protocol.py` | Host-side protocol implementation (pure, tested) |
| `tactidose/hardware/conformance.json` | Protocol scenarios shared by the simulator, firmware and real-board tests |
| `tactidose/db/` | Data model (SQLite locally, TiDB in the cloud), analytics outbox |
| `tactidose/core/` | Settings, clock (demo time travel), event bus, cross-module interfaces |
| `tests/` | pytest suite and shared fakes |

## Development setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[all,dev]"
pytest
```

All cloud services (Gemini, ElevenLabs, TiDB, Snowflake) are optional; copy
`.env.example` to `.env` to configure them.

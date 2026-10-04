# TactiDose.tech

A pill dispenser app for people who struggle to manage their medication (including blind and
low-vision users). An ESP32 drops one pill at a time from one of **3 containers** — automatically
at scheduled times, when the patient presses **Drop pill**, or when the patient asks the **AI
assistant** by voice or text. Every drop is logged, pill counts go down, a **global cooldown**
stops repeated drops, and doctors and family members see everything in their own portal, including
**PDF reports** they can email to the doctor.

> **Hackathon prototype — not a medical device.** It does not diagnose, prescribe or change
> doses. Demo with candy or labelled tokens only, never real medication.

## What it does

| Feature | How it works |
|---|---|
| Scheduled drops | Doctor/family set times per pill. At that time the pill drops by itself, even if the patient forgets. |
| Drop button | The patient can drop a pill from the app; a **global cooldown** (default 60 min, set by doctor/family) blocks another manual drop of any pill. |
| No double doses | A scheduled dose counts as done if the same pill dropped shortly before it (within the dose window or the 60-minute minimum interval). |
| Pill counts | Each container's count goes down on every drop; low-stock and empty alerts go to the patient and their care team. |
| AI assistant | The patient talks (voice or text). It checks the history and asks for a pill only when allowed — the same deterministic rules as the button decide. Gemini when a key is set, otherwise an offline rule-based assistant. |
| Conversation log | Only the **patient's** conversations are stored, including what the assistant looked up and asked for. |
| Reports | A PDF for the last N days (adherence, drops, refused requests, inventory, a factual summary of conversations) stored in the database, viewable in both portals, emailable to the doctor. |
| Two portals | **Patient** portal and **doctor/family** portal, with login. Caregivers link to a patient using the patient's ID + link code. Only doctor/family can change schedules, cooldown, containers and medications. |
| Safety | AI never controls the motor. Uncertain drops (e.g. the USB cable is pulled mid-drop) are flagged and block further drops until a caregiver checks. |

## Quick start (no hardware needed)

Requires Python 3.10+ (3.12 tested). On Windows PowerShell:

```powershell
git clone https://github.com/ArmaanChahal/TactiDose.tech.git
cd TactiDose.tech
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[all,dev]"
python -m tactidose download-voice-model     # offline speech recognition (~40 MB), optional
python -m tactidose run --sim                # simulated ESP32
```

macOS/Linux: `source .venv/bin/activate` instead of the activate line.

Open **http://127.0.0.1:8000** and sign in with a demo account (password `demo1234`):

| Account | Role | Sees |
|---|---|---|
| `alex@demo.tactidose` | patient | patient portal: drop buttons, assistant, schedule, history, reports |
| `sam@demo.tactidose` | family | care portal for Alex |
| `dr.lee@demo.tactidose` | doctor | care portal for Alex; receives report emails |

The **demo panel** (`/demo`) has a demo clock ("jump to the next dose"), simulator controls
(faults, pill counts, button presses) and one-click checklists for the four demo flows. See
[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md).

Run `python -m tactidose doctor` any time to check the setup (database, serial ports, voice model,
audio devices, which cloud services are configured).

## Connecting the real ESP32

1. Wire the board and set the pins and mechanism in `firmware/tactidose_esp32/config.h`
   (carousel + one release servo, or one servo per container — both supported). Follow
   [docs/HARDWARE_INTEGRATION.md](docs/HARDWARE_INTEGRATION.md) for wiring and power.
2. Flash `firmware/tactidose_esp32` (Arduino IDE / arduino-cli, ESP32 core 3.x, libraries
   AccelStepper and ESP32Servo; `firmware/compile_esp32.ps1` compiles it in Docker).
3. Plug it in and run the checklist (it moves the device and drops pills — load candy first):
   ```powershell
   python -m tactidose ports                   # the ESP32's port is auto-detected by USB ID
   python -m tactidose hw-test --port COM5     # home, every container, drops, stop, bad command
   python -m tactidose conformance --target serial --port COM5   # protocol checks
   ```
4. Start the app on the real device: `python -m tactidose run --serial auto` (or `--serial COM5`).

The serial protocol is in [docs/SERIAL_PROTOCOL.md](docs/SERIAL_PROTOCOL.md). Firmware that only
supports the older v1 commands still works: the app drops a pill with `DISPENSE_SLOT` + `CLOSE_GATE`.

## Configuration

Copy `.env.example` to `.env`. Everything is optional; without keys the full demo runs offline.

| Service | Settings | Without it |
|---|---|---|
| Gemini (assistant, report summary, label scanning) | `GEMINI_API_KEY`, `GEMINI_MODEL` | offline rule-based assistant and summaries |
| ElevenLabs (natural voice) | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | built-in OS voice |
| Email (send reports) | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` (Gmail: an app password) | emails are saved as `.eml` files in `data/outbox` |
| TiDB (cloud database) | `TIDB_HOST`, `TIDB_USER`, `TIDB_PASSWORD`, `TIDB_DATABASE` | local SQLite in `data/` |
| Snowflake (analytics, optional) | `SNOWFLAKE_*` | analytics stay local |

Useful app settings: `TACTIDOSE_MANUAL_COOLDOWN_MINUTES` (default for new devices),
`TACTIDOSE_AUTO_DROP_ENABLED`, `TACTIDOSE_NUM_SLOTS` (3), `TACTIDOSE_TIMEZONE`,
`TACTIDOSE_DEMO_MODE`, `TACTIDOSE_HARDWARE_MODE` (`sim` / `serial` / `none`).

## How it works

```
Patient (voice / text / Drop button)        Doctor / family (care portal)
          │                                          │
          ▼                                          ▼
   AI assistant ──requests──►  DropService  ◄── schedules, cooldown, refills
   (Gemini or rules)          deterministic rules: device ready · medication set up ·
                              no pending review · global cooldown · dose not already
                              satisfied · pills left · one drop at a time
                                     │ DROP_SLOT n
                                     ▼
                              ESP32 (serial) ──► OK DROPPED n / ERR NO_PILL
                                     │
           database: pill_drops · pill counts · doses · conversations · reports · notifications
                                     │
              live notifications (SSE) · PDF reports · email to the doctor
```

Design rule: **AI interprets, deterministic code authorizes and actuates.** The assistant can only
*ask* `DropService` for a pill; every rule is checked again there. Details:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/API.md](docs/API.md).

## Commands

| Command | What it does |
|---|---|
| `python -m tactidose run [--sim \| --serial PORT \| --no-hardware] [--no-voice] [--port 8000]` | start the web app |
| `python -m tactidose doctor` | check configuration and environment |
| `python -m tactidose seed-demo` / `reset-demo` | create / reset the demo accounts and data |
| `python -m tactidose create-user`, `link`, `bind-device` | manage accounts, caregiver links and the device owner |
| `python -m tactidose hw-test`, `conformance`, `ports`, `simulator` | hardware checks, protocol tests, port list, TCP simulator |
| `python -m tactidose generate-report --patient-id 1 --days 7` | make a PDF report from the command line |
| `python -m tactidose send-test-email --to you@example.com` | check the email setup |
| `python -m tactidose download-voice-model`, `warm-tts-cache` | offline voice setup |

## Testing

```powershell
pytest                       # ~2,000 tests, about 2 minutes
pytest -m "not native"       # skip the Docker-based firmware tests
python -m tactidose conformance --target sim      # 32 protocol scenarios on the simulator
python -m tactidose conformance --target native   # the same against the real firmware code (Docker)
```

## Project layout

```
tactidose/
  medication/   drops (rules, inventory, auto-drops), scheduler, notifications, compartments, catalog
  agent/        AI assistant (Gemini + rules), tools, conversation store, voice in/out
  reports/      statistics, narrative, PDF, email
  auth/         accounts, sessions, roles, caregiver links
  api/ app.py   FastAPI routes, live events, startup
  hardware/     serial client, ESP32 simulator, protocol, self-test
  voice/ audio/ offline speech recognition, text-to-speech
  ui/static/    login, patient portal, care portal, demo panel, kiosk
firmware/       reference ESP32 firmware + native test harness
docs/           architecture, API, serial protocol, hardware integration, demo script
tests/          pytest suite
```

## Troubleshooting

- **The app stops responding when the laptop sleeps.** Keep it plugged in and awake during a demo.
- **No ESP32 found.** `python -m tactidose ports` lists ports; boards without a known USB bridge
  need `--serial COMx`. Install the CP210x/CH340 driver if Windows shows no COM port.
- **The board resets when the app connects.** Normal for many ESP32 boards; the app waits for it
  to boot and home.
- **Voice input says "not available".** Run `python -m tactidose download-voice-model`, or use the
  browser microphone (Chrome/Edge) in the patient portal.
- **Gemini / ElevenLabs fail on a corporate network.** Proxies often block them; the app falls back
  to the offline assistant and voice automatically.
- **The repository is in OneDrive/Dropbox.** Prefer a normal folder: sync clients can lock the
  SQLite database and slow everything down.

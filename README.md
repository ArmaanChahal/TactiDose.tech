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
| Well-being check-in (optional) | After a pill drops, the patient is asked whether they want a quick check-in (mood, stress, sleep, support, with optional notes in their own words). Saved check-ins are stored next to the pill history, shown in a **Well-being check-ins** section of History in both portals, linked to the pill they followed. Non-clinical, never affects pills. Lives in [`tactidose-wellbeing/`](tactidose-wellbeing/README.md). |
| Safety | AI never controls the motor. Uncertain drops (e.g. the USB cable is pulled mid-drop) are flagged and block further drops until a caregiver checks. |

## Quick start (no hardware needed)

Requires Python 3.10+ (3.12 tested). On Windows PowerShell:

```powershell
git clone https://github.com/ArmaanChahal/TactiDose.tech.git
cd TactiDose.tech
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[all,dev]"
pip install -e "./tactidose-wellbeing[dev]"  # optional well-being check-in
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

### Guided judge demo (morning, noon, night)

A voice-first run of one demo day in about a minute, with candy, not medicine. It works fully
offline with the simulator and the rule-based assistant, and sounds better with `GEMINI_API_KEY` /
`ELEVENLABS_API_KEY` in `.env`.

1. `python -m tactidose warm-tts-cache` once (pre-renders every fixed demo line, optional), then
   `python -m tactidose run --sim --demo-pause-seconds 7`.
2. Screen for the judges: open **http://127.0.0.1:8000/kiosk** signed in as `alex@demo.tactidose`.
3. Operator: open **/demo** signed in as `sam@demo.tactidose` (another browser or a private window),
   keep **Start from fresh demo data** ticked and press **Run guided demo** (Alex can also press it
   on the kiosk, without the fresh-data option).
4. For each slot the kiosk asks "It's time for your morning pill. Do you want to take it?", listens
   automatically and answers by voice. If the microphone or Wi-Fi fails, type the answer in the
   **Type an answer** box (kiosk or demo panel).
   * **Yes:** "I'm turning on the buzzer…" plays a beeping (simulated) buzzer and drops the pill
     through the normal drop rules. Then "Did you take the pill?".
   * **No:** the dose is skipped. An unclear answer is asked once more, then counts as no.
   * Then "How has your day been? How are you feeling? Any problems?". Mood, symptoms, concerns
     and severity are read from the answer (rules, or Gemini when configured, always checked).
     Emergency wording ("chest pain", "can't breathe") gets the fixed emergency reply, notifies the
     care team and stops the demo.
5. After the night pill: a goodbye and a spoken summary of the three slots. **Stop demo** (or the
   kiosk's Stop button, or saying "stop") ends it at any time and stops the dispenser.

**Buzzer.** By default the screen beeps (the laptop tone). For a real buzzer on the ESP32 set
`TACTIDOSE_BUZZER_BACKEND=serial` (or `both`) and the pin in the firmware's `config.h`, then check
it with `python -m tactidose buzzer-test`. The step-by-step checklist, including how to roll back,
is in [docs/BUZZER.md](docs/BUZZER.md).

How it fits in: each slot is that container's *scheduled dose* (08:00 / 13:00 / 20:00). The demo
clock jumps forward to 15 minutes before each one, and the drop is
`DropService.request_drop(source="schedule")`, so every normal rule applies and the manual
cooldown is never involved. Results: `guided_demo_slots`, `pill_drops`, `dose_events` and a
"Guided demo" conversation. Rehearse without a browser:

```bash
python -m tactidose guided-demo                    # scripted answers, prints every line and the stored rows
python -m tactidose guided-demo --answers "yes|yes|Good day|no|Fine|no|Tired"
```

Run `python -m tactidose doctor` any time to check the setup (database, serial ports, voice model,
audio devices, which cloud services are configured).

## Cloud services and API keys (optional)

Everything runs offline without any keys: the rule-based assistant, the computer's built-in voice,
a local SQLite database, and report emails saved as files. Each key you add turns on one cloud
service. If that service fails later (no internet, quota used up, a blocked network), the app falls
back to its offline behaviour by itself.

**Where the keys go:** in a file named `.env` in the project folder (the folder you run
`python -m tactidose` from). Create it from the template (skip the copy if you already have a
`.env`: it would be overwritten), then fill in only the lines you need:

```powershell
Copy-Item .env.example .env      # macOS/Linux: cp .env.example .env
notepad .env
```

`.env` is git-ignored. **Never commit keys** and never put them in `.env.example`. Write values
without quotes (`GEMINI_API_KEY=AIza...`). A real environment variable with the same name overrides
the line in `.env`. **Restart the server after editing `.env`**, because settings are only read at
start-up.

| Service | Lines to set in `.env` | Where to get it | What it turns on |
|---|---|---|---|
| **Gemini** (start here) | `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) (free) | The assistant understands free-form speech and text instead of fixed phrases. It also writes the factual conversation summary in reports and reads medication labels from photos. |
| ElevenLabs | `ELEVENLABS_API_KEY`, optional `ELEVENLABS_VOICE_ID` | elevenlabs.io → Developers → API Keys; give the key **Text to Speech** access | A natural voice for spoken replies instead of the built-in one |
| Email (Gmail) | `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER`, `SMTP_PASSWORD` (a Gmail **app password**), `SMTP_FROM` | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (needs 2-Step Verification) | Reports are really emailed to the doctor; without it they are saved as `.eml` files in `data/outbox` |
| TiDB | `TIDB_HOST`, `TIDB_USER`, `TIDB_PASSWORD`, `TIDB_DATABASE`, then run `python -m tactidose init-db` once | TiDB Cloud → your cluster → Connect | The app's database lives in TiDB Cloud instead of local SQLite |
| Snowflake | `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_TOKEN` (a programmatic access token), `SNOWFLAKE_WAREHOUSE`, `TACTIDOSE_ANALYTICS_SALT` (a long random string) | your Snowflake account (a trial works) | De-identified adherence and device events are synced to Snowflake for analytics (tables are created automatically) |

To keep a key in `.env` but stop using it: `TACTIDOSE_AGENT_PROVIDER=rules` (offline assistant),
`TACTIDOSE_REPORT_AI_SUMMARY=false` (rule-based report summary), `TACTIDOSE_LABEL_EXTRACTOR=disabled`
(no label scanning), `TACTIDOSE_TTS_PROVIDER=offline` (built-in voice). TiDB, Snowflake and email
are off while `TIDB_HOST`, `SNOWFLAKE_ACCOUNT` and `SMTP_HOST` are empty.

### Test your keys

```powershell
python -m tactidose check-apis                           # every configured service
python -m tactidose check-apis --only gemini,elevenlabs  # just these
python -m tactidose check-apis --json                    # machine-readable
```

The first line shows which settings file was read: `Settings file: C:\...\.env (found)`. If it says
*not found*, you are in the wrong folder. Then there is one row per service (`gemini`,
`gemini-agent`, `elevenlabs`, `snowflake`, `tidb`, `smtp`) with a status. When something needs
fixing, a `->` line underneath says what to do. The command sends a tiny live request or two to
each service you configured; services without settings are not contacted, and no email is sent.
It exits with code 0 unless a configured service failed.

| Status | Meaning |
|---|---|
| `OK` | The service answered and the key works. |
| `WARN` | It works, with a caveat (for example a fallback model or another voice was used). Read the `->` line. |
| `NOT_CONFIGURED` | No key yet. The row lists the `.env` lines to add; until then the app uses its offline fallback. |
| `INVALID_KEY` | The key was rejected. Copy the whole key again (no quotes or spaces) and check it was not revoked. |
| `QUOTA_EXCEEDED` | Rate limit, daily quota or credits used up. Wait a minute, use a smaller model, or check your plan. |
| `BLOCKED_BY_NETWORK` | A web filter on this network blocked or redirected the request. The app did not follow the redirect, so the key was not sent on. Use a network that allows the service, or ask its administrator. |
| `TLS_ERROR` | A TLS-inspecting proxy re-signs HTTPS on this network: see the certificate note below. |
| `NOT_FOUND` | The model or voice ID in `.env` does not exist for this key (for TiDB: run `init-db`). |

Other codes (`PERMISSION_DENIED`, `TIMEOUT`, `NETWORK_ERROR`, `BAD_REQUEST`, `SERVER_ERROR`, `ERROR`)
come with their own `->` hint.

Then check it in the app:

1. Start (or restart) it: `python -m tactidose run --sim`.
2. Sign in as the patient (`alex@demo.tactidose`) and talk to the assistant in your own words.
3. Sign in as family or doctor (`sam@demo.tactidose` / `dr.lee@demo.tactidose`) and open
   **Conversations**. Each assistant reply shows the model that wrote it:
   - `Assistant (gemini-3.8-flash)`: Gemini answered;
   - `rules (fallback)`: Gemini failed (or is paused after a recent failure), so the offline
     assistant answered;
   - `rules`: no Gemini key (or `TACTIDOSE_AGENT_PROVIDER=rules`);
   - `rules (safety)`: a fixed safety answer instead of Gemini (an emergency, "stop", or a Gemini
     reply that did not match what the device did).
4. Open http://127.0.0.1:8000/api/health. The `agent` part shows `provider` (`gemini` or `rules`),
   `model` and the circuit breaker: `gemini_retry_in_s` is the number of seconds until Gemini is
   tried again after a failure (0 = not paused), and `gemini_last_error` is the status of the last
   failure (for example `BLOCKED_BY_NETWORK`).

### Notes on networks, privacy and quotas

- **Work and school networks** often block or redirect AI APIs. The app never follows a redirect
  while sending a key; it reports `BLOCKED_BY_NETWORK` and uses the offline assistant and voice
  instead. After a Gemini failure the offline assistant answers at once for
  `TACTIDOSE_AGENT_RETRY_AFTER_S` seconds (default 60; `0` = try Gemini on every message), so
  replies don't keep waiting for a timeout.
- **TLS-inspecting proxies** re-sign HTTPS traffic with their own root certificate. Point the
  environment variables `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` at a CA bundle that includes that
  root. Python reads these from the environment, not from `.env`, so set them in Windows ("Edit
  environment variables for your account"), or in the PowerShell window before starting the app:
  `$env:SSL_CERT_FILE="C:\path\bundle.pem"; $env:REQUESTS_CA_BUNDLE=$env:SSL_CERT_FILE`. TiDB uses
  `TIDB_SSL_CA` if set, otherwise the file named by `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`,
  otherwise the certifi bundle.
- **Privacy:** on Gemini's free tier, Google may use prompts to improve its products and people may
  review them. Use demo data only, never real patient information. A TLS-inspecting proxy can also
  read your keys in transit, so use keys you can revoke and revoke them after the event.
- **ElevenLabs voice:** the default voice ("George", `JBFqnCBsd6RMkjVDRZzb`) is a legacy voice that
  accounts created after March 2026 cannot use. With `TACTIDOSE_ELEVENLABS_AUTO_VOICE=true` (the
  default), the app switches to your account's first premade voice and logs which
  `ELEVENLABS_VOICE_ID` to set. Picking a voice means listing your voices, which some networks block,
  so you can also set `ELEVENLABS_VOICE_ID` yourself (copy an ID from the ElevenLabs Voices page).
  Free accounts may be refused on shared or proxied networks (`PERMISSION_DENIED`).
- **Gemini quota:** the free tier's daily quota may be small. When it runs out (`QUOTA_EXCEEDED`),
  the offline assistant answers until it resets. Setting `GEMINI_MODEL` to a Flash-Lite model
  (e.g. `gemini-flash-lite-latest`) usually gives higher free limits; `check-apis` confirms the
  model exists. `TACTIDOSE_AGENT_THINKING_LEVEL=low` makes spoken replies faster (empty = the
  model's default).

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

### Connecting the ESP32 over Wi-Fi

The ESP32 dispenser on the network has a static IP, **`http://172.20.10.9`**, and three endpoints:

| Action | ESP32 endpoint | Who, where in the website |
|---|---|---|
| Dispense pill 1, 2, 3 | `GET /dispense?pill=1` (`2`, `3`) | **Patient**: **Dispense pill 1 / 2 / 3** buttons (Home, Pill device card) and the **Drop pill** button on each container. The assistant and scheduled doses use it too |
| Open the lid (restock) | `GET /lid?state=open` | **Doctor / family**: care portal → Containers → **Restock the dispenser** → **Open lid to restock** |
| Close the lid | `GET /lid?state=close` | **Doctor / family**: same card → **Close lid** |

**Dispensing** only calls `/dispense?pill=N`. Container 1 is `pill=1`, and the lid is never touched.
The app's drop rules run first: the global cooldown, the double-dose guard, pill counts, and the
history and notifications. A pill can't be dropped from the website without them.

**Restocking** is separate and is a doctor/family task, like refills:
1. Open the lid.
2. Put the pills in.
3. Record the new count with **Refill** on each container.
4. Close the lid.

The lid buttons never dispense. Patients don't see them; the API refuses them with 403.

What the ESP32's answer means:

| ESP32 answer | Recorded as |
|---|---|
| HTTP 200 | **dropped** (pill count −1) |
| Another HTTP status | **failed** (nothing dropped) |
| Not reachable | refused as **device unavailable** (nothing was sent) |
| Reached, but no answer within 20 s | **uncertain**: further drops wait until a caregiver checks (History → "It dropped" / "It did not drop") |

The app checks every 5 s that the ESP32 is reachable (`GET /`) and shows it as online or offline.

**Run it:**
```bash
python -m tactidose run --wifi                        # uses http://172.20.10.9
python -m tactidose run --wifi http://192.168.1.60    # another address for this run
python -m tactidose doctor                            # "the ESP32 answered" when it is reachable
```
To make Wi-Fi the default, put `TACTIDOSE_HARDWARE_MODE=wifi` in `.env`. Optionally add
`TACTIDOSE_ESP32_URL=http://<ip>` if the board gets a different address.

**Changing the IP or endpoints:** everything is in one file,
[`tactidose/hardware/wifi_config.py`](tactidose/hardware/wifi_config.py):
* `ESP32_BASE_URL` (the static IP)
* `LID_OPEN_PATH`, `LID_CLOSE_PATH`, `DISPENSE_PATH` (`{pill}` = container 1–3)
* `HEALTH_PATH`, `METHOD`
* the timeouts

The ESP32's endpoints have no stop, home or buzzer command. Over Wi-Fi the **Stop** button
therefore cannot halt a dispense in progress, and the buzzer setting falls back to the laptop tone.
Keep the ESP32 and the computer on the same trusted Wi-Fi; the endpoints have no password.

## Configuration

Copy `.env.example` to `.env`. Everything is optional; without keys the full demo runs offline.
Cloud keys and how to test them: [Cloud services and API keys](#cloud-services-and-api-keys-optional).

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
| `python -m tactidose run [--sim \| --serial PORT \| --wifi [URL] \| --no-hardware] [--no-voice] [--port 8000]` | start the web app (`--wifi`: the ESP32 at http://172.20.10.9) |
| `python -m tactidose doctor` | check configuration and environment |
| `python -m tactidose check-apis [--only gemini,elevenlabs,snowflake,tidb,smtp] [--json]` | test the cloud keys in `.env` with one tiny live request per configured service |
| `python -m tactidose init-db` | create the database and tables (run once after setting `TIDB_*`) |
| `python -m tactidose seed-demo` / `reset-demo` | create / reset the demo accounts and data |
| `python -m tactidose create-user`, `link`, `bind-device` | manage accounts, caregiver links and the device owner |
| `python -m tactidose hw-test`, `conformance`, `ports`, `simulator` | hardware checks, protocol tests, port list, TCP simulator |
| `python -m tactidose generate-report --patient-id 1 --days 7` | make a PDF report from the command line |
| `python -m tactidose send-test-email --to you@example.com` | check the email setup |
| `python -m tactidose download-voice-model`, `warm-tts-cache` | offline voice setup |
| `python -m tactidose guided-demo [--answers "yes\|yes\|…"] [--pause N]` | headless guided judge demo on the simulator (own data folder) |
| `python -m tactidose buzzer-test [--backend serial] [--serial PORT \| --sim]` | sound the buzzer for 2 s and report what the device answered (docs/BUZZER.md) |

## Testing

```powershell
pytest                       # ~2,000 tests, about 2 minutes
pytest -m "not native"       # skip the Docker-based firmware tests
cd tactidose-wellbeing && pytest   # the check-in package's own suite (138 tests)
python -m tactidose conformance --target sim      # 40 protocol scenarios on the simulator (32 + 8 buzzer)
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
  hardware/     serial client, ESP32 simulator, protocol, self-test, Wi-Fi ESP32 driver (wifi_config.py = IP + endpoints)
  voice/ audio/ offline speech recognition, text-to-speech
  ui/static/    login, patient portal, care portal, demo panel, kiosk
  wellbeing.py  host-side bridge to the optional check-in (identity, chat routing, /api/wellbeing)
  guided/       guided judge demo: MORNING / NOON / NIGHT state machine, free-text check-in extraction
tactidose-wellbeing/  standalone well-being check-in package (own pyproject, tests, docs)
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
- **Gemini / ElevenLabs fail on a work network.** Run `python -m tactidose check-apis`.
  `BLOCKED_BY_NETWORK` means a web filter blocks the service, and `TLS_ERROR` means a TLS-inspecting
  proxy (see the notes under [Cloud services and API keys](#cloud-services-and-api-keys-optional)).
  The app falls back to the offline assistant and voice automatically.
- **A key in `.env` seems to be ignored.** Restart the server, and check that `check-apis` prints
  the `.env` you edited (`Settings file: ... (found)`).
- **The repository is in OneDrive/Dropbox.** Prefer a normal folder: sync clients can lock the
  SQLite database and slow everything down.

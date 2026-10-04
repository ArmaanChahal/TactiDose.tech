# Learnings and handoff — read this first (next Claude session)

Everything a new session needs to continue TactiDose without re-discovering it. Written at the end
of the first build session (2026-10-03). Newest status is in §6 and §9.

## 1. What this is

TactiDose.tech is a 24-hour hackathon prototype (**not a medical device**, demo with candy only): an
ESP32 drops one pill at a time from **3 containers**; a FastAPI app (patient portal, doctor/family
portal, demo panel) decides *whether* a pill may drop; an AI assistant (Gemini, offline rules
fallback) lets the patient ask by voice or text; PDF reports go to the doctor. Repo:
https://github.com/ArmaanChahal/TactiDose.tech (branch `main`). Start with `README.md`, then
`docs/ARCHITECTURE.md` (v2), `docs/API.md` (v2), `docs/SERIAL_PROTOCOL.md` (v1.1).

The original spec is `TactiDose_Hardware_Software_Handoff.md`. **The owner changed the product on
2026-10-03** (pill drops, global cooldown, agent, reports, two portals) — where the handoff conflicts
with `docs/ARCHITECTURE.md`, the architecture doc wins.

## 2. Product decisions made by the owner (do not silently change)

| Decision | Choice |
|---|---|
| Mechanism | 3 containers; the host sends `DROP_SLOT n` (one pill). Carousel+trapdoor *or* one servo per container — both supported in firmware (`MECHANISM_*` in `config.h`). |
| Scheduled doses | Drop automatically at their time even if the patient forgets. |
| Cooldown | **One global cooldown**: after any drop, button/AI drops of *any* pill are refused for N minutes (doctor/family set N, may be 0). Scheduled drops ignore it. |
| Per-pill floor | **Added after review, owner-approved:** the same pill can never drop twice within 60 min from the button/AI/demo, even with cooldown 0 (`min_dose_interval_minutes`). |
| Double-dose guard | A scheduled dose counts as done if the same pill dropped within max(early window, 60 min) before it. |
| Agent | Voice + text. It may only *request* a pill; deterministic rules decide. Gemini is the provider (already in the stack). Only **patient** conversations are stored (incl. tool calls). |
| Editing rights | Only doctor/family edit schedules, cooldown, containers/refills, medications. Patient views and requests. |
| Linking | Caregivers link with the patient's database ID **plus** a link code shown in the patient portal. |
| Reports | PDF stored in the DB (`reports.pdf`), viewable in both portals, email to linked doctors (SMTP, or `.eml` in `data/outbox` without SMTP). |
| Old handoff features | Gemini label scanning, Snowflake analytics, TiDB option, kiosk screen: kept as **optional extras** behind config. |
| Demo mode | Off by default after the security review; `python -m tactidose run --sim --demo` for demos. |

## 3. Where things are

```
tactidose/medication/drops.py    THE ONLY code path that drops pills (DropService): every rule, inventory,
                                 auto-drops, uncertain-drop review lock, notifications. ~2k lines.
tactidose/medication/scheduler.py schedules -> dose_events (DST-safe), MISSED transitions
tactidose/agent/                 AgentService, tools (bound to one patient), gemini_agent, rules_agent,
                                 prompts, voice (Vosk STT, TTS), voice_loop (optional device mic loop)
tactidose/hardware/protocol.py   wire format + drop_certainty() (DROPPED / NOT_DROPPED / UNCERTAIN)
tactidose/hardware/serial_client.py  HardwareClient (owns the SimulatedDevice in sim mode)
tactidose/hardware/simulator.py  VirtualESP32 twin of the firmware (32 conformance scenarios)
tactidose/reports/               stats, narrative (Gemini or rules), fpdf2 PDF, mailer
tactidose/auth/                  scrypt passwords, sessions (ignore demo clock travel), roles, links
tactidose/api/ + app.py          routes per docs/API.md, per-user SSE filtering, scheduler thread
tactidose/ui/static/             login.html, patient.html, care.html, demo.html, kiosk.html (vanilla JS)
firmware/tactidose_esp32/        reference firmware (portable core + Arduino HAL); firmware/native = test harness
tests/                           ~2,000 pytest tests; tests/fakes.py (FakeDropHardware, seed_v2)
```

Wiring lives in `tactidose/app.py` (`build_services`). Rules are listed in `docs/ARCHITECTURE.md` §5
(check order) and §13 (binding facts from the build).

## 4. Run and test on this laptop

- Python venv is **outside OneDrive**: `C:\tmp\tactidose-venv\Scripts\python.exe` (Python 3.12,
  package installed editable). Install changes with
  `uv pip install --python C:\tmp\tactidose-venv\Scripts\python.exe --link-mode=copy <pkg>`
  (uv's default hardlinks fail on this filesystem).
- Run: `python -m tactidose run --sim --no-voice` → http://127.0.0.1:8000 (demo mode is on by
  default via `TACTIDOSE_DEMO_MODE=true`; there is no `--demo` flag), logins
  `alex@demo.tactidose` (patient), `sam@demo.tactidose` (family), `dr.lee@demo.tactidose` (doctor),
  password `demo1234`. Use a data dir outside OneDrive for experiments:
  `$env:TACTIDOSE_DATA_DIR="C:\tmp\tactidose-integ\data"`.
- Voice model already downloaded at `C:\tmp\tactidose-models\vosk-model-small-en-us-0.15`
  (set `TACTIDOSE_VOSK_MODEL_PATH`). Test recordings (16 kHz WAV, Windows voice) in
  `C:\tmp\tactidose-scratch\voice_wavs`.
- End-to-end smoke test against a running server: `C:\tmp\tactidose-scratch\integ\smoke.py` (35 HTTP
  checks: auto-drop, cooldown, permissions, agent, report, email) — adjust port/env if needed.
- Browser checks: Playwright lives in `C:\tmp\tactidose-scratch\ui\.venv` (bundled Chromium; the
  corporate policy blocks remote debugging of installed Chrome/Edge). Script:
  `C:\tmp\tactidose-scratch\integ\ui_check.py`.
- Tests: `pytest -q -m "not native"` (~2 min); native firmware tests need Docker (`-m native`);
  conformance: `python -m tactidose conformance --target sim|native`.
- `python -m tactidose doctor` summarises the environment.

## 5. Verified vs not verified

| Verified (this session) | Not verified |
|---|---|
| Full suite green; 32/32 protocol scenarios on the Python simulator and the native firmware core | **Nothing has run on a real ESP32 yet** (no board attached) |
| Real app end to end with the simulator: auto-drop, cooldown refusal, per-role permissions, agent chat, PDF report, email saved as .eml, demo clock jump | Gemini, ElevenLabs, SMTP, TiDB, Snowflake with real credentials (all mocked) |
| Real Vosk recognition of recorded commands; spoken replies via Windows SAPI; WAV served | Real microphone/speaker by ear; Firefox/Safari; screen readers (NVDA/JAWS) |
| Firmware compiles for ESP32 (6 variants, esp32 core 3.3.12) | Firmware behaviour on hardware: pins, servo angles, steps, brownouts, drop sensor |

## 6. Safety reviews and what was fixed

Two independent reviews ran at the end (a multi-agent review in this session plus a separate Claude
chat). Confirmed findings and their status:

| Finding | Status |
|---|---|
| Race: a drop still in flight was reported as "a pill was dropped at …" (cooldown check) | **fixed** |
| Manual drop just before a dose's early window → second pill at the dose time | **fixed** (lookback = max(early window, 60 min)) |
| Fuzzy name match ("vitamin d" dropped Vitamin C; first word only) | **fixed** — every word of the name must match (`agent/tools.py` `name_score`) |
| Agent requested pills on deferrals/questions ("tonight", "later", "after dinner", "with milk?", "should I take…?") | **fixed** — `turn_guard` blocks `request_pill` for every provider (emergency, unclear, injection, negation, several pills, medication change, deferral, non-due question) |
| Emergencies → 911 reply; replies claiming a drop that did not happen | already deterministic in `AgentService` (verified by tests); not re-reviewed |
| Bare negated fragment ("not the calcium") | **open for Gemini**: the rules agent does not drop, but the guard does not hard-block it |
| Gemini fallback path could actuate | **not re-verified** (one drop request per message is enforced in the tool executor) |
| Demo panel usable by any signed-in account; demo drops skipped the cooldown; reset by anyone | **fixed differently**: demo mode stays ON by default for the hackathon, but the panel is limited to the device's patient and linked doctor/family; console and reset are doctor/family only; demo drops obey the global cooldown and the per-pill floor; `DISPENSE_SLOT`/`OPEN_GATE` refused. Set `TACTIDOSE_DEMO_MODE=false` for anything beyond a demo |
| Same pill twice within 60 min when caregivers set the cooldown to 0 | **fixed** — per-pill floor (`min_dose_interval_minutes`, default 60) for app/agent/button/demo drops, reason `COOLDOWN` |
| Reset/brown-out while the trapdoor opens recorded as "nothing dropped" | **fixed** — `ERR STOPPED`/`DEVICE_RESET` after `OK AT_SLOT` → UNCERTAIN (needs review) |
| Device slot count ≠ app setting only warned | open |
| No-home-sensor carousel re-zeroes at a random position after any reset | open (`alignment_required` until a caregiver confirms Home was the plan) |
| Drop-sensor miss → retry could drop a second pill | open — mitigated: `ERR NO_PILL` sets the count to 0, so retries are refused as EMPTY until a refill |
| Report email to any address; default Snowflake salt; personal replies cached on disk; health endpoint detail; analytics by device | open |
| No CI; `vosk>=0.3.45` breaks macOS | open |

Reviews that were **stopped before finishing** (to save time): dosing-safety deep dive, accessibility,
platform robustness. Re-run them (see §8) before anything beyond a demo.

## 7. Environment gotchas on this machine

- **The laptop sleeps after 5 min idle and the corporate policy cuts the network in standby** — this
  froze every agent for 40 min once. A keep-awake helper (`C:\tmp\tactidose-scratch\keep-awake.ps1`,
  `SetThreadExecutionState`, stops itself after 6 h or when `keepawake.stop` exists) fixed it. No admin
  rights (`powercfg /requests` fails).
- Repo is inside a **OneDrive-synced** folder: keep venv, SQLite data and Docker-heavy work outside it.
  Git works but line-ending warnings are normal (`.gitattributes` normalises to LF).
- **PowerShell 5.1** mangles inline Python (`-c "..."` quoting): write scripts to files. Git Bash
  heredocs break on apostrophes in content. Windows console is cp1252: set `PYTHONIOENCODING=utf-8`
  when printing Unicode.
- Corporate TLS-inspecting proxy: Docker containers need its root CA for downloads
  (`firmware/compile_esp32.ps1 -CaSubject …`); some AI-vendor docs/APIs are blocked by the web filter
  (ElevenLabs docs were). The app falls back offline automatically.
- `COM3` is an Intel AMT serial port, not an ESP32 — auto-detect ignores ports without a known USB ID.
- tzdata 2026 encodes **permanent daylight time for British Columbia**: America/Vancouver has no
  November 2026 fall-back. DST tests use America/Los_Angeles.
- Session-scoped cron jobs (`CronCreate`/one-shot wakeups) did **not fire** in this VS Code session;
  background `Start-Sleep 600` commands (they notify on exit) worked as a reliable timer.
- Docker volume `tactidose-arduino` (~8 GB) caches the ESP32 toolchain; `docker volume rm` to reclaim.

## 8. Process learnings (how to work on this effectively)

- **Contracts first, then parallel agents** worked well: freezing protocol/data model/interfaces/API
  docs before fanning out let 6–8 agents build modules that integrated with only two seam bugs.
- **Where time went** (measured from transcripts): ~74% model writing/thinking, ~10% running tests,
  and in wave 1 a 40-minute freeze from laptop sleep. Per-agent stress tests and browser screenshot
  marathons were the avoidable part. Time-box agents and do stress testing once at integration.
- **Integration on the real app finds what unit tests miss** (the cooldown race showed up only in the
  full-suite run under load). Run `smoke.py` and `ui_check.py` after every round.
- **Adversarial review is mandatory for the safety paths.** The agent's natural-language handling was
  the weakest area (negations, deferrals, partial names); the demo panel and reset-mid-release holes
  were found by two independent reviewers. Prefer structural gates (a deterministic action gate in
  front of every pill action) over phrase-by-phrase patches.
- Workflow agents cannot be messaged mid-run; put all guidance in the prompt (time box, no stress
  loops, file ownership, "report contract issues instead of editing frozen files").
- Keep `docs/ARCHITECTURE.md` §13 updated with binding facts after each round — agents rely on it.

## 9. Open items and next steps

1. **Real hardware bring-up** (tonight): set pins/mechanism in `firmware/tactidose_esp32/config.h`,
   flash, `python -m tactidose hw-test --port COMx`, then `run --serial auto`. Separate servo
   supply + bulk capacitor (brown-outs mark drops UNCERTAIN and block further drops by design).
2. Remaining review items: the **open** rows in §6 (slot-count mismatch, no-home-sensor alignment,
   report email restrictions, Snowflake salt, TTS disk cache, health redaction, CI, vosk pin, the
   Gemini negated-fragment case). Ask the other Claude chat to re-review the fix commit.
3. Re-run the stopped reviews (dosing, accessibility, platform); then a human read of `drops.py`,
   `agent/tools.py`, `agent/rules_agent.py`, `hardware/protocol.py` before any real-world use.
4. Keys to add when available: `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`, SMTP (Gmail app password).
5. Nice-to-have: split `drops.py`; tolerant STATUS parsing for teammates' own firmware written from the
   handoff (§14 never defined the STATUS format); simulator turns the carousel the short way round,
   the firmware does not (timing only).

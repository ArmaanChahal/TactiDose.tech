# TactiDose v2 design (draft)

On 2026-10-03 the product structure changed: the device **drops pills** from **3 containers**,
the schedule **auto-drops** doses, one **global cooldown** limits manual/agent drops, an **AI
agent** (voice + text) talks with the patient, **PDF reports** can be emailed to the doctor, and
there are **patient** and **doctor/family** portals with login.

These documents describe that design. They are being merged into `docs/` and the code; until
then, where they disagree with `docs/ARCHITECTURE.md`, `docs/API.md` or the original handoff,
**the v2 documents win**.

| File | Contents |
|---|---|
| `ARCHITECTURE.md` | Drop rules, cooldown, double-dose guard, inventory, agent tools, reports, accounts and permissions |
| `API.md` | HTTP API v2 (auth, patient data, drops, agent, reports, notifications) |
| `SERIAL_PROTOCOL_v1.1.md` | `DROP_SLOT n` command for the ESP32 (backwards compatible with v1 firmware) |

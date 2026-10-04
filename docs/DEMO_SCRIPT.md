# TactiDose demo script (about 5 minutes)

Audience: the demo presenter and operator. Use candy or labelled tokens only.

## Before you start (5 minutes ahead)

1. Laptop plugged in, sleep disabled, volume up.
2. Start the app:
   - with the real device: `python -m tactidose run --serial auto`
   - without it: `python -m tactidose run --sim`
3. `python -m tactidose reset-demo` for a clean slate (signs everyone out).
4. Open three browser windows (or tabs):
   - **Patient** — `http://127.0.0.1:8000/login` → `alex@demo.tactidose` / `demo1234`
   - **Doctor** — a private window → `dr.lee@demo.tactidose` / `demo1234`
   - **Operator** — `http://127.0.0.1:8000/demo` (demo clock, simulator, checklists)
5. Real device: fill the 3 containers with candy, run `python -m tactidose hw-test --port COMx`
   once, and check the counts in the doctor portal (Containers tab) match what you loaded.

## The story

> "Alex has three medications. Remembering times, telling bottles apart and knowing whether a pill
> was already taken is hard — especially with low vision. TactiDose drops the right pill at the
> right time, lets Alex ask for one by voice, and stops double doses."

### A. A scheduled pill drops by itself (1 min)
1. Operator: **Jump to the next dose** in the demo clock.
2. The device drops the pill; the patient window shows a **"Pill dropped"** notification and the
   container's count goes down; the doctor's Overview updates live.
3. Say: "Alex didn't have to remember anything — the schedule the doctor set did it."

### B. The Drop button and the cooldown (1 min)
1. In the doctor window, Cooldown tab: set the cooldown to **2 minutes** for the demo.
2. Patient window: press **Drop pill** on a container → it drops.
3. Press **Drop pill** again (any container) → refused: *"A pill was dropped at 8:01 AM. The next
   pill can drop at 8:03 AM."* Nothing moved.
4. Say: "One global cooldown — no accidental repeat drops, and the reason is spoken and shown."

### C. Ask the assistant (1–2 min)
1. Wait for the cooldown to end (or set it to 0 in the doctor window).
2. Patient window → **Assistant**: press the mic (or type) *"Can I have my calcium?"* → it checks
   the history and the calcium drops.
3. Ask again → it refuses and explains the cooldown.
4. Ask *"How many pills are left?"* → it reads the counts.
5. Doctor window → **Conversations**: the whole conversation, including what the assistant looked
   up and asked for, is there.
6. Say: "The AI only *asks* — the same safety rules as the button decide. And it never gives
   medical advice; for symptoms it tells you to contact your doctor or call 911."

### D. Report to the doctor (1 min)
1. Doctor window → **Reports** → last 7 days → **Generate**.
2. Open the PDF: adherence, drops, refused requests, inventory, conversation summary.
3. **Send to doctor** → with SMTP configured it arrives by email; without it the email is saved in
   `data/outbox` (show the `.eml`).

### Optional extras
- **Uncertain drop:** operator → simulator → *brownout on release* fault, then drop a pill → the
  drop is marked *uncertain* and further drops are blocked until the doctor resolves it in History.
- **Empty container:** operator → set a container's simulated pills to 0 → drop → *"container is
  empty"* + alert to the care team.
- **Kiosk screen** (`/kiosk`): a large, voice-first device screen for the patient.

## If something goes wrong

| Problem | Do this |
|---|---|
| Device shows "not ready" | Doctor → Device tab → **Home**; check USB; `python -m tactidose doctor` |
| A drop is "uncertain" and everything is blocked | Doctor → History → resolve it ("It dropped" / "It did not drop") |
| No voice | Type in the assistant instead; voice is optional |
| Clock confusion after jumping | Operator → **Back to real time** (or `reset-demo`) |
| Demo data messy | `python -m tactidose reset-demo`, sign in again |

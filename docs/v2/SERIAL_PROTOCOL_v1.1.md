
## 12. v1.1 — `DROP_SLOT` (pill drop) **(added 2026-10-03)**

The v2 product drops **one pill** from one of **3 containers** instead of presenting a compartment at
an open gate. v1.1 adds one command; everything above is unchanged, so v1 firmware keeps working
(the host emulates a drop, see 12.5).

### 12.1 Command

```
DROP_SLOT <n>
```

Drop exactly one pill from container `n` (0-based; user-facing "container n+1") into the output
chute. Device-side macro: gate/release closed → move to slot `n` (designs with a fixed release per
container skip the motion and report it immediately) → settle → **release** (open, hold
`DROP_OPEN_MS`, close — atomic, ≤ ~1.5 s) → optional drop-sensor check → report.

### 12.2 Output

From `READY`:

```
OK MOVING n
OK AT_SLOT n
OK GATE_OPEN
OK GATE_CLOSED
OK DROPPED n        ← terminal success
OK READY
```

Failures: every `MOVE_SLOT` failure (`ERR INVALID_SLOT`, `NOT_HOMED`, `BUSY`, `INVALID_STATE`,
`MOTOR_FAULT`, `STOPPED`, `UNKNOWN_COMMAND`) plus:

| Message | Meaning |
|---|---|
| `ERR NO_PILL` | Only with a drop sensor: no pill passed the sensor during the release (container empty or jammed). Sent after `OK GATE_CLOSED`; the device then returns to `READY` (`OK READY`). |

Acceptance (§7): same row as `DISPENSE_SLOT`. Argument validation first (`ERR INVALID_SLOT` in every state).

### 12.3 Interruption

* `STOP` / cancel button **during motion or settle** → `ERR STOPPED`, `OK STOPPED` — the release
  never happened.
* The release itself is atomic: a `STOP` received during it is processed after `OK DROPPED n` /
  `ERR NO_PILL` and `OK READY`.
* Host rule: if `ERR STOPPED` or a reset (`EVENT BOOT`) arrives **after** `OK GATE_OPEN`, a pill may
  have dropped → the host records the drop as **UNCERTAIN** (fail closed).

### 12.4 STATUS additions

`OK STATUS … proto=1.1 drop_sensor=<0|1>` — v1 devices omit `proto`. Hosts must ignore unknown keys.

### 12.5 Host behaviour

* Device reports `proto ≥ 1.1` → the host sends `DROP_SLOT n` (timeout 30 s).
* Otherwise (v1 firmware) → `DISPENSE_SLOT n`, wait `drop_close_delay_ms` (default 1.5 s), `CLOSE_GATE`.
* `OK DROPPED n` → the host decrements that container's pill count. `ERR NO_PILL` → the host sets it
  to 0 and notifies "container empty". No sensor → `OK DROPPED` means "release cycle completed".

### 12.6 Hardware mapping guidance

| Mechanism | `MOVE`/`AT_SLOT` | "gate" (release) |
|---|---|---|
| Carousel (stepper) over one chute + trapdoor servo | stepper positions container `n` over the chute | trapdoor servo |
| 3 fixed containers, one dispensing wheel/servo each | no motion (report `OK MOVING n`, `OK AT_SLOT n` immediately) | servo of container `n` rotates one pocket |
| Drop sensor (optional, recommended) | – | IR break-beam in the chute sampled during the release |

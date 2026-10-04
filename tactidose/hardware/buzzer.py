"""Buzzer interface and backends ("follow the sound to the table").

Callers (the guided demo runner, the ``buzzer-test`` CLI) depend only on :class:`Buzzer`:

* ``on(ms=None)`` - start sounding for ``ms`` (default ``buzzer_config.DEFAULT_ON_MS``, capped at
  ``buzzer_config.MAX_ON_MS``); stops by itself after that. Returns True if something sounds.
* ``off()`` - stop now. Safe to call when already off.
* ``supports_hardware`` - True for backends that drive the device's buzzer.
* ``tone_active`` - the laptop tone should be sounding now (the kiosk / demo screen beeps while
  the runner's ``demo.guided`` events say ``buzzer: true``).
* ``hardware_active`` - the device's buzzer was switched on and not yet off.
* ``add_listener(callback)`` - called with the buzzer after every change (UI publication).

Backends (:func:`create_buzzer`, setting ``TACTIDOSE_BUZZER_BACKEND``):

=========  ===============================================================================
laptop     :class:`LaptopToneBuzzer` - today's behaviour: a beeping tone from the screen.
serial     :class:`SerialBuzzer` - ``BUZZER ON <ms>`` / ``BUZZER OFF`` through the HardwareClient;
           any failure (old firmware, no buzzer fitted, timeout, busy link, exception) logs one
           warning and falls back to the laptop tone.
both       :class:`BothBuzzer` - device buzzer and laptop tone together.
none       :class:`NullBuzzer` - silent.
=========  ===============================================================================

No method raises, and nothing here touches DropService: a buzzer failure can never block a drop.
A serial ``on()`` takes at most ``COMMAND_TIMEOUT_S x (1 + RETRIES)`` before returning.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
from typing import Any, Callable

from tactidose.hardware import buzzer_config

log = logging.getLogger(__name__)

__all__ = ["BACKENDS", "BothBuzzer", "Buzzer", "LaptopToneBuzzer", "NullBuzzer", "SerialBuzzer", "create_buzzer"]

BACKENDS = ("laptop", "serial", "both", "none")
#: Replies after which the device is known not to have a buzzer (until it reconnects / reboots).
_UNSUPPORTED = frozenset({"UNKNOWN_COMMAND", "NO_BUZZER"})
#: Host-side results worth one more try.
_RETRYABLE = frozenset({"TIMEOUT", "BUSY_LOCAL"})


def _duration(ms: int | None) -> int:
    value = buzzer_config.DEFAULT_ON_MS if ms is None else int(ms)
    return max(1, min(value, buzzer_config.MAX_ON_MS))


class Buzzer:
    """Interface + listener plumbing shared by the backends."""

    name = "buzzer"

    def __init__(self) -> None:
        self._listeners: list[Callable[["Buzzer"], None]] = []
        self._listen_lock = threading.Lock()

    @property
    def supports_hardware(self) -> bool:
        return False

    @property
    def tone_active(self) -> bool:
        return False

    @property
    def hardware_active(self) -> bool:
        return False

    def on(self, ms: int | None = None) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def off(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        self.off()

    def add_listener(self, callback: Callable[["Buzzer"], None]) -> Callable[[], None]:
        with self._listen_lock:
            self._listeners.append(callback)

        def remove() -> None:
            with self._listen_lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return remove

    def _changed(self) -> None:
        with self._listen_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(self)
            except Exception:  # noqa: BLE001 - a UI listener must never break the buzzer
                log.exception("buzzer listener failed")

    def status(self) -> dict[str, Any]:
        return {"backend": self.name, "supports_hardware": self.supports_hardware,
                "tone_active": self.tone_active, "hardware_active": self.hardware_active}


class NullBuzzer(Buzzer):
    """Silent backend (``none``)."""

    name = "none"

    def on(self, ms: int | None = None) -> bool:
        return False

    def off(self) -> None:
        return None


class LaptopToneBuzzer(Buzzer):
    """The laptop / screen tone (``laptop``). Moved here from the runner unchanged in behaviour: while
    ``tone_active`` is True the kiosk and demo screen beep. It switches itself off after ``ms``.

    ``play_locally=True`` (the ``buzzer-test`` CLI) also beeps through this computer's speaker with
    ``tactidose.audio.playback`` when it is available (no browser needed)."""

    name = "laptop"

    def __init__(self, *, play_locally: bool = False) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._on = False
        self._timer: threading.Timer | None = None
        self._play_locally = play_locally
        self._player: Any = None

    @property
    def tone_active(self) -> bool:
        with self._lock:
            return self._on

    def on(self, ms: int | None = None) -> bool:
        duration = _duration(ms)
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(duration / 1000.0, self._expire)
            self._timer.daemon = True
            self._timer.start()
            was_on, self._on = self._on, True
        if self._play_locally:
            self._play(duration)
        if not was_on:
            self._changed()
        return True

    def off(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            was_on, self._on = self._on, False
        self._stop_local()
        if was_on:
            self._changed()

    def _expire(self) -> None:
        with self._lock:
            self._timer = None
            was_on, self._on = self._on, False
        if was_on:
            self._changed()

    # ------------------------------------------------------------------ local speaker (CLI)
    def _play(self, duration_ms: int) -> None:
        try:
            from tactidose.audio.playback import Player

            self._player = self._player or Player()
            wav = _beep_wav(duration_ms)
            threading.Thread(target=self._player.play_wav, args=(wav,), name="buzzer-tone", daemon=True).start()
        except Exception:  # noqa: BLE001 - no audio device: the terminal bell at least
            print("\a", end="", flush=True)

    def _stop_local(self) -> None:
        stop = getattr(self._player, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:  # noqa: BLE001
                pass


class SerialBuzzer(Buzzer):
    """The device's buzzer over the serial protocol (``serial``), with a fallback backend.

    Unsupported (``ERR UNKNOWN_COMMAND`` / ``ERR NO_BUZZER``) is remembered until the device
    reboots or reconnects; timeouts and a busy link are retried ``RETRIES`` times. Every failure
    logs a warning and uses ``fallback`` (the laptop tone unless told otherwise)."""

    name = "serial"

    def __init__(self, hardware: Any, *, fallback: Buzzer | None = None) -> None:
        super().__init__()
        self.hardware = hardware
        self.fallback = fallback if fallback is not None else LaptopToneBuzzer()
        self.fallback.add_listener(lambda _b: self._changed())
        self._lock = threading.Lock()
        self._hw_on = False
        self._unsupported_for: object = None   # device identity the "no buzzer" verdict is for
        self.last_error: str | None = None

    @property
    def supports_hardware(self) -> bool:
        return True

    @property
    def tone_active(self) -> bool:
        return self.fallback.tone_active

    @property
    def hardware_active(self) -> bool:
        with self._lock:
            return self._hw_on

    def on(self, ms: int | None = None) -> bool:
        duration = _duration(ms)
        problem = self._hardware_on(duration)
        if problem is None:
            return True
        level = logging.INFO if problem.startswith("device without a buzzer") else logging.WARNING  # once loud
        log.log(level, "buzzer: device buzzer not used (%s); using the %s instead", problem,
                "laptop tone" if isinstance(self.fallback, LaptopToneBuzzer) else self.fallback.name)
        return self.fallback.on(duration)

    def off(self) -> None:
        with self._lock:
            was_on, self._hw_on = self._hw_on, False
        if was_on:
            try:
                result = self.hardware.buzzer_off()
                if not getattr(result, "ok", False):
                    log.info("buzzer: BUZZER OFF -> %s (it stops by itself at its max time)",
                             getattr(result, "code", "?"))
            except Exception:  # noqa: BLE001
                log.exception("buzzer: BUZZER OFF failed")
            self._changed()
        self.fallback.off()

    def _identity(self) -> object:
        try:
            snap = self.hardware.snapshot()
            return (getattr(snap, "fw_version", None), getattr(snap, "resets_seen", None),
                    getattr(snap, "connected", None))
        except Exception:  # noqa: BLE001
            return None

    def _hardware_on(self, duration: int) -> str | None:
        """Switch the device's buzzer on; None on success, else why not."""
        send = getattr(self.hardware, "buzzer_on", None)
        if not callable(send):
            return "this hardware has no buzzer support"
        identity = self._identity()
        if self._unsupported_for is not None and self._unsupported_for == identity:
            return f"device without a buzzer ({self.last_error})"
        code = "?"
        for _attempt in range(1 + max(0, int(buzzer_config.RETRIES))):
            try:
                result = send(duration)
            except Exception as exc:  # noqa: BLE001 - never let the buzzer break the caller
                self.last_error = type(exc).__name__
                return f"error {self.last_error}"
            if getattr(result, "ok", False):
                with self._lock:
                    self._hw_on = True
                self.last_error = None
                self._changed()
                return None
            code = str(getattr(result, "code", "?"))
            self.last_error = code
            if code in _UNSUPPORTED:
                self._unsupported_for = identity
                return f"device answered {code}"
            if code not in _RETRYABLE:
                break
        return f"BUZZER ON failed: {code}"


class BothBuzzer(Buzzer):
    """Device buzzer and laptop tone together (``both``)."""

    name = "both"

    def __init__(self, hardware: Any) -> None:
        super().__init__()
        self.laptop = LaptopToneBuzzer()
        self.serial = SerialBuzzer(hardware, fallback=NullBuzzer())
        for part in (self.laptop, self.serial):
            part.add_listener(lambda _b: self._changed())

    @property
    def supports_hardware(self) -> bool:
        return True

    @property
    def tone_active(self) -> bool:
        return self.laptop.tone_active

    @property
    def hardware_active(self) -> bool:
        return self.serial.hardware_active

    def on(self, ms: int | None = None) -> bool:
        laptop = self.laptop.on(ms)
        device = self.serial.on(ms)
        return laptop or device

    def off(self) -> None:
        self.serial.off()
        self.laptop.off()


def create_buzzer(settings: Any, hardware: Any = None, *, play_locally: bool = False) -> Buzzer:
    """The backend for ``settings.buzzer_backend`` (unknown values: the laptop tone)."""
    backend = str(getattr(settings, "buzzer_backend", "laptop") or "laptop").lower()
    if backend == "none":
        return NullBuzzer()
    if backend in ("serial", "both") and hardware is None:
        log.warning("buzzer backend %r needs the hardware client; using the laptop tone", backend)
        backend = "laptop"
    if backend == "serial":
        return SerialBuzzer(hardware, fallback=LaptopToneBuzzer(play_locally=play_locally))
    if backend == "both":
        both = BothBuzzer(hardware)
        both.laptop._play_locally = play_locally
        return both
    return LaptopToneBuzzer(play_locally=play_locally)


def _beep_wav(duration_ms: int, *, rate: int = 16000, freq: float = 880.0) -> bytes:
    """A beeping 16-bit mono WAV (250 ms tone / 350 ms gap) for the CLI's local speaker."""
    samples = bytearray()
    total = int(rate * duration_ms / 1000)
    period = int(rate * 0.6)
    tone = int(rate * 0.25)
    for i in range(total):
        value = int(9000 * math.sin(2 * math.pi * freq * i / rate)) if (i % period) < tone else 0
        samples += struct.pack("<h", value)
    header = b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(samples))
    return header + bytes(samples)

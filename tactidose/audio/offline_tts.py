"""Offline OS speech synthesis with no extra Python dependencies.

* **Windows**: PowerShell + ``System.Speech.Synthesis.SpeechSynthesizer`` (SAPI). The
  script is a *static* constant passed with ``-EncodedCommand``. The output path and the
  text reach it only as base64 lines on stdin, never interpolated into the script, so no
  user text can be executed. Output goes to a temporary WAV
  (``SetOutputToWaveFile``), which keeps playback uniform and interruptible.
  ``-NoProfile -NonInteractive``, and no console window (``CREATE_NO_WINDOW``).
* **macOS**: ``say -o out.wav --file-format=WAVE --data-format=LEI16@22050 -f -``
  (text on stdin).
* **Linux**: ``espeak-ng`` (or ``espeak``) ``-w out.wav --stdin``.

:meth:`OfflineTTS.synthesize` returns WAV bytes or ``None`` and never raises. Each call is
bounded by ``timeout_s``. After repeated failures the engine is benched for a while, so a
broken voice install does not add seconds of latency to every utterance.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from tactidose.audio.playback import is_wav

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 2000
_FAILURES_BEFORE_BACKOFF = 3
_BACKOFF_S = 300.0

#: Reads 4 base64 lines from stdin: output path, text, voice name (optional), rate (optional).
_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$lines = [Console]::In.ReadToEnd() -split "`r?`n"
function Dec([string]$b64) {
    if ([string]::IsNullOrWhiteSpace($b64)) { return '' }
    return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($b64.Trim()))
}
$out = Dec $lines[0]
$text = Dec $lines[1]
$voice = ''
$rate = ''
if ($lines.Count -gt 2) { $voice = Dec $lines[2] }
if ($lines.Count -gt 3) { $rate = Dec $lines[3] }
if (-not $out -or -not $text) { exit 2 }
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if ($voice) { try { $synth.SelectVoice($voice) } catch { } }
    if ($rate) { $synth.Rate = [Math]::Max(-10, [Math]::Min(10, [int]$rate)) }
    $synth.SetOutputToWaveFile($out)
    $synth.Speak($text)
} finally {
    $synth.Dispose()
}
exit 0
"""
_PS_ENCODED = base64.b64encode(_PS_SCRIPT.encode("utf-16-le")).decode("ascii")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _no_window_kwargs(platform: str) -> dict[str, Any]:
    if platform != "win32":
        return {}
    kwargs: dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        si = startupinfo_cls()
        si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        si.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = si
    return kwargs


class OfflineTTS:
    """Operating-system text-to-speech rendered to WAV bytes."""

    def __init__(self, *, timeout_s: float = 30.0, voice: str | None = None,
                 rate: int | None = None, platform: str | None = None) -> None:
        self.platform = platform or sys.platform
        self.timeout_s = float(timeout_s)
        self.voice = voice
        self.rate = rate
        self._lock = threading.Lock()
        self._detected = False
        self._exe: str | None = None
        self._engine: str | None = None
        self._failures = 0
        self._benched_until = 0.0
        self.last_error: str | None = None

    # ------------------------------------------------------------------ discovery
    def _detect(self) -> None:
        with self._lock:
            if self._detected:
                return
            self._detected = True
            try:
                if self.platform == "win32":
                    exe = shutil.which("powershell") or shutil.which("powershell.exe")
                    if exe is None:
                        root = os.environ.get("SystemRoot", r"C:\Windows")
                        candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
                        exe = str(candidate) if candidate.is_file() else None
                    self._exe, self._engine = exe, ("windows-sapi" if exe else None)
                elif self.platform == "darwin":
                    exe = shutil.which("say")
                    self._exe, self._engine = exe, ("macos-say" if exe else None)
                else:
                    for name in ("espeak-ng", "espeak"):
                        exe = shutil.which(name)
                        if exe:
                            self._exe, self._engine = exe, name
                            break
            except Exception as exc:  # noqa: BLE001
                log.warning("offline TTS detection failed: %s", exc)
                self._exe = self._engine = None
            if self._engine:
                log.info("offline speech engine: %s (%s)", self._engine, self._exe)
            else:
                log.info("no offline speech engine found on %s", self.platform)

    @property
    def engine(self) -> str | None:
        """``"windows-sapi"`` | ``"macos-say"`` | ``"espeak-ng"`` | ``"espeak"`` | ``None``."""
        self._detect()
        return self._engine

    def available(self) -> bool:
        self._detect()
        return self._engine is not None and time.monotonic() >= self._benched_until

    def status(self) -> dict[str, Any]:
        return {"engine": self.engine, "available": self.available(), "error": self.last_error}

    # ------------------------------------------------------------------ synthesis
    def _command(self, text: str, out_path: str) -> tuple[list[str], bytes]:
        exe = self._exe or ""
        if self._engine == "windows-sapi":
            payload = "\n".join((
                _b64(out_path), _b64(text), _b64(self.voice or ""),
                _b64("" if self.rate is None else str(int(self.rate))),
            )) + "\n"
            cmd = [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", _PS_ENCODED]
            return cmd, payload.encode("ascii")
        if self._engine == "macos-say":
            cmd = [exe, "-o", out_path, "--file-format=WAVE", "--data-format=LEI16@22050"]
            if self.voice:
                cmd += ["-v", self.voice]
            if self.rate is not None:
                cmd += ["-r", str(int(self.rate))]
            return cmd + ["-f", "-"], text.encode("utf-8")
        cmd = [exe, "-w", out_path, "--stdin", "-b", "1"]
        if self.voice:
            cmd += ["-v", self.voice]
        if self.rate is not None:
            cmd += ["-s", str(int(self.rate))]
        return cmd, text.encode("utf-8")

    def synthesize(self, text: str) -> bytes | None:
        """Render ``text`` to WAV bytes. ``None`` when unavailable or on any failure."""
        cleaned = " ".join(str(text or "").split())[:MAX_TEXT_CHARS]
        if not cleaned or not self.available():
            return None
        out_path: str | None = None
        try:
            fd, out_path = tempfile.mkstemp(prefix="tactidose-offline-", suffix=".wav")
            os.close(fd)
            cmd, stdin = self._command(cleaned, out_path)
            started = time.monotonic()
            proc = subprocess.run(
                cmd, input=stdin, capture_output=True, timeout=self.timeout_s, check=False,
                **_no_window_kwargs(self.platform),
            )
            if proc.returncode != 0:
                tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
                return self._failed(f"{self._engine} exited with {proc.returncode}: {tail}")
            data = Path(out_path).read_bytes()
            if not is_wav(data) or len(data) <= 44:
                return self._failed(f"{self._engine} produced no audio")
            self._failures = 0
            self.last_error = None
            log.debug("offline TTS rendered %d chars in %.2f s", len(cleaned), time.monotonic() - started)
            return data
        except subprocess.TimeoutExpired:
            return self._failed(f"{self._engine} timed out after {self.timeout_s:g} s")
        except Exception as exc:  # noqa: BLE001 - never raise into the speaker
            return self._failed(f"{type(exc).__name__}: {exc}")
        finally:
            if out_path:
                try:
                    os.unlink(out_path)
                except OSError:
                    pass

    def _failed(self, reason: str) -> None:
        self.last_error = reason
        self._failures += 1
        if self._failures >= _FAILURES_BEFORE_BACKOFF:
            self._benched_until = time.monotonic() + _BACKOFF_S
            self._failures = 0
            log.warning("offline TTS failing repeatedly (%s); disabled for %d s", reason, int(_BACKOFF_S))
        else:
            log.warning("offline TTS failed: %s", reason)
        return None

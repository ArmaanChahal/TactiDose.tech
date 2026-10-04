"""Offline OS speech (tactidose/audio/offline_tts.py) with subprocess mocked.

The real Windows SAPI path can be smoke-tested with ``TD_SAPI_SMOKE=1`` (skipped by default).
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tactidose.audio import offline_tts
from tactidose.audio.offline_tts import _PS_ENCODED, _PS_SCRIPT, OfflineTTS
from tactidose.audio.playback import pcm_to_wav, read_wav

WAV = pcm_to_wav(b"\x01\x00" * 800, 22050)


class FakeRun:
    """Stands in for subprocess.run: records the call and writes a WAV to the output path."""

    def __init__(self, *, returncode: int = 0, wav: bytes | None = WAV, exc: BaseException | None = None,
                 stderr: bytes = b"") -> None:
        self.returncode, self.wav, self.exc, self.stderr = returncode, wav, exc, stderr
        self.calls: list[dict[str, Any]] = []

    def out_path(self, cmd: list[str], stdin: bytes) -> str:
        if "-EncodedCommand" in cmd:
            return base64.b64decode(stdin.decode("ascii").splitlines()[0]).decode("utf-8")
        flag = "-o" if "-o" in cmd else "-w"
        return cmd[cmd.index(flag) + 1]

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append({"cmd": cmd, **kwargs})
        if self.exc is not None:
            raise self.exc
        path = self.out_path(cmd, kwargs["input"])
        assert os.path.exists(path), "the output file is created before the engine runs"
        if self.wav is not None:
            Path(path).write_bytes(self.wav)
        return subprocess.CompletedProcess(cmd, self.returncode, b"", self.stderr)


@pytest.fixture
def which(monkeypatch):
    table: dict[str, str | None] = {}
    monkeypatch.setattr(offline_tts.shutil, "which", lambda name: table.get(name))
    return table


@pytest.fixture
def run(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(offline_tts.subprocess, "run", fake)
    return fake


def decode_stdin(stdin: bytes) -> list[str]:
    return [base64.b64decode(line).decode("utf-8") if line else "" for line in stdin.decode("ascii").split("\n")]


# --------------------------------------------------------------------------- Windows / SAPI


def test_windows_sapi_command_is_hidden_static_and_text_goes_via_stdin(which, run):
    which["powershell"] = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    tts = OfflineTTS(platform="win32", timeout_s=12)
    assert tts.available() and tts.engine == "windows-sapi"
    text = "Your Caf\u00e9 (demo) is ready. $(Remove-Item C:\\x) `whoami` 'q' \"dq\""
    wav = tts.synthesize(text)
    assert wav == WAV
    call = run.calls[0]
    cmd = call["cmd"]
    assert cmd[0] == which["powershell"]
    assert "-NoProfile" in cmd and "-NonInteractive" in cmd
    assert cmd[cmd.index("-EncodedCommand") + 1] == _PS_ENCODED
    script = base64.b64decode(_PS_ENCODED).decode("utf-16-le")
    assert script == _PS_SCRIPT and "Remove-Item" not in script and "Caf" not in script
    assert all("Caf" not in part for part in cmd)  # the text never appears on the command line
    lines = decode_stdin(call["input"])
    assert lines[1] == " ".join(text.split())  # exact text, safely transported as base64
    assert lines[0].endswith(".wav")
    assert call["timeout"] == 12 and call["capture_output"] is True
    assert call["creationflags"] & 0x08000000  # CREATE_NO_WINDOW
    assert "SetOutputToWaveFile" in _PS_SCRIPT and "System.Speech" in _PS_SCRIPT
    assert not os.path.exists(lines[0])  # temp WAV removed


def test_windows_voice_and_rate_are_passed_as_data(which, run):
    which["powershell"] = "powershell.exe"
    tts = OfflineTTS(platform="win32", voice="Microsoft Zira Desktop", rate=-2)
    assert tts.synthesize("Hello.") is not None
    lines = decode_stdin(run.calls[0]["input"])
    assert lines[2] == "Microsoft Zira Desktop" and lines[3] == "-2"


def test_same_static_script_for_every_text(which, run):
    which["powershell"] = "powershell.exe"
    tts = OfflineTTS(platform="win32")
    tts.synthesize("one")
    tts.synthesize("two")
    assert run.calls[0]["cmd"] == run.calls[1]["cmd"]


def test_windows_falls_back_to_system32_path(which, run, monkeypatch, tmp_path):
    exe = tmp_path / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    tts = OfflineTTS(platform="win32")
    assert tts.engine == "windows-sapi"
    tts.synthesize("Hi.")
    assert run.calls[0]["cmd"][0] == str(exe)


# --------------------------------------------------------------------------- macOS / Linux


def test_macos_say_command(which, run):
    which["say"] = "/usr/bin/say"
    tts = OfflineTTS(platform="darwin", voice="Samantha")
    assert tts.synthesize("Cancelled.") == WAV
    cmd = run.calls[0]["cmd"]
    assert cmd[:2] == ["/usr/bin/say", "-o"]
    assert "--file-format=WAVE" in cmd and "--data-format=LEI16@22050" in cmd
    assert cmd[-2:] == ["-f", "-"] and ["-v", "Samantha"] == cmd[cmd.index("-v"):cmd.index("-v") + 2]
    assert run.calls[0]["input"] == b"Cancelled."
    assert "creationflags" not in run.calls[0]


@pytest.mark.parametrize("available,engine", [({"espeak-ng": "/usr/bin/espeak-ng"}, "espeak-ng"),
                                               ({"espeak": "/usr/bin/espeak"}, "espeak")])
def test_linux_espeak_command(which, run, available, engine):
    which.update(available)
    tts = OfflineTTS(platform="linux")
    assert tts.engine == engine
    assert tts.synthesize("Hello there.") == WAV
    cmd = run.calls[0]["cmd"]
    assert cmd[0] == available[engine] and cmd[1] == "-w" and "--stdin" in cmd
    assert run.calls[0]["input"] == "Hello there.".encode("utf-8")


# --------------------------------------------------------------------------- failures never raise


def test_unavailable_engine(which, run, monkeypatch, tmp_path):
    monkeypatch.setenv("SystemRoot", str(tmp_path / "nowhere"))
    tts = OfflineTTS(platform="win32")
    assert not tts.available() and tts.engine is None
    assert tts.synthesize("Hello.") is None
    assert run.calls == []
    assert OfflineTTS(platform="linux").available() is False


@pytest.mark.parametrize(
    "fake,reason",
    [
        (FakeRun(returncode=1, stderr=b"Add-Type : Cannot add type"), "exited with 1"),
        (FakeRun(wav=None), "produced no audio"),
        (FakeRun(wav=b"RIFF....WAVE"), "produced no audio"),
        (FakeRun(exc=subprocess.TimeoutExpired(["powershell"], 30)), "timed out"),
        (FakeRun(exc=FileNotFoundError("powershell.exe")), "FileNotFoundError"),
    ],
)
def test_failures_return_none(which, monkeypatch, fake, reason):
    which["powershell"] = "powershell.exe"
    monkeypatch.setattr(offline_tts.subprocess, "run", fake)
    tts = OfflineTTS(platform="win32")
    assert tts.synthesize("Hello.") is None
    assert reason in (tts.last_error or "")


def test_repeated_failures_bench_the_engine(which, monkeypatch):
    which["powershell"] = "powershell.exe"
    fake = FakeRun(returncode=1)
    monkeypatch.setattr(offline_tts.subprocess, "run", fake)
    tts = OfflineTTS(platform="win32")
    for _ in range(3):
        assert tts.synthesize("Hello.") is None
    assert tts.available() is False
    assert tts.synthesize("Hello.") is None and len(fake.calls) == 3


def test_empty_text_is_not_rendered(which, run):
    which["powershell"] = "powershell.exe"
    tts = OfflineTTS(platform="win32")
    assert tts.synthesize("   ") is None and run.calls == []


def test_long_text_is_capped(which, run):
    which["powershell"] = "powershell.exe"
    OfflineTTS(platform="win32").synthesize("word " * 1000)
    assert len(decode_stdin(run.calls[0]["input"])[1]) <= offline_tts.MAX_TEXT_CHARS


@pytest.mark.skipif(sys.platform != "win32" or not os.environ.get("TD_SAPI_SMOKE"),
                    reason="real SAPI smoke test: set TD_SAPI_SMOKE=1 on Windows")
def test_real_windows_sapi_smoke():  # pragma: no cover - manual
    wav = OfflineTTS().synthesize("Cancelled.")
    assert wav is not None and read_wav(wav).duration_s > 0.3

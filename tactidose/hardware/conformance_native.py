"""NativeTarget: runs the conformance suite against the natively compiled firmware core.

``firmware/native/bin/harness`` wraps ``firmware/tactidose_esp32/TactiDoseCore.cpp`` (the exact
state machine that runs on the ESP32) in a fake HAL that simulates the carousel physics of
``conformance.json`` ("harness") in simulated time. It speaks the stdin/stdout protocol of
docs/ARCHITECTURE.md §7. This module implements
:class:`~tactidose.hardware.conformance.ConformanceTarget` on top of it::

    python -m tactidose.hardware.conformance_native build     # (re)build the harness binary
    python -m tactidose.hardware.conformance --target native  # run every scenario

The binary is a static Linux ELF built with g++ in Docker (``firmware/native/build.ps1`` /
``build.sh``). On Linux it runs directly. On Windows and macOS it runs through
``docker run -i --rm -v <repo>:/work:ro -w /work <image> /work/firmware/native/bin/harness``.
``TACTIDOSE_NATIVE_HARNESS_CMD`` replaces that whole command line (for example
``wsl /path/to/harness``) and ``TACTIDOSE_NATIVE_HARNESS_IMAGE`` replaces the image (default
``gcc:14``, the build image). One harness process serves a whole runner session;
``reset()`` sends ``!reset`` before every scenario.

Speed: the frozen runner ticks in 5 ms chunks, and a Docker pipe round trip costs ~2 ms, so naive
ticking would need ~80,000 round trips (minutes) for the suite. With ``speculate=True`` (the
default) every exchange ends with ``!peek``: "how many ms stay silent if no input arrives?".
The harness computes that on a snapshot of the simulation and then restores it. ``tick()`` calls
inside the silent window are answered locally and the time accumulates. That time is sent as a
single ``!tick`` before the next input, or as soon as a tick could produce output. So the
firmware sees every input at the same simulated millisecond as with naive ticking, and every line
comes back from the same ``tick()`` call. ``speculate=False`` restores one round trip per call;
the tests use it to cross-check.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from types import TracebackType
from typing import IO, Sequence

log = logging.getLogger(__name__)

__all__ = [
    "NativeTarget", "HarnessError", "HarnessBuildError", "REPO_ROOT", "DEFAULT_BINARY", "DEFAULT_IMAGE",
    "ENV_COMMAND", "ENV_IMAGE", "resolve_binary", "harness_sources", "harness_is_stale", "build_harness",
    "docker_unavailable_reason", "main",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BINARY = "firmware/native/bin/harness"
DEFAULT_IMAGE = "gcc:14"
ENV_COMMAND = "TACTIDOSE_NATIVE_HARNESS_CMD"
ENV_IMAGE = "TACTIDOSE_NATIVE_HARNESS_IMAGE"
BUILD_SCRIPT = "firmware/native/build.sh"
CONTAINER_ROOT = "/work"
_SOURCE_DIRS = ("firmware/tactidose_esp32", "firmware/native")
_SOURCE_SUFFIXES = (".h", ".cpp", ".sh")
_PEEK_START_MS = 1000
_PEEK_MAX_MS = 131072
_BOOT_MODES = ("ok", "dead", "none")
_SENSOR_MODES = ("ok", "dead", "stuck")
_BUTTONS = ("CONFIRM", "CANCEL")


class HarnessError(RuntimeError):
    """The harness process could not start, exited, timed out or rejected a directive."""


class HarnessBuildError(HarnessError):
    """Compiling the harness binary failed."""


# --------------------------------------------------------------------------- binary / build helpers


def resolve_binary(binary: str | os.PathLike[str] = DEFAULT_BINARY) -> Path:
    """Absolute harness path: as given, else relative to the CWD, else relative to the repo root."""
    path = Path(binary)
    if path.is_absolute():
        return path
    cwd_candidate = Path.cwd() / path
    return cwd_candidate if cwd_candidate.exists() else REPO_ROOT / path


def harness_sources(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Files the harness binary is built from (firmware core + native harness + build script)."""
    files: list[Path] = []
    for rel in _SOURCE_DIRS:
        folder = repo_root / rel
        if folder.is_dir():
            files.extend(p for p in folder.iterdir() if p.is_file() and p.suffix in _SOURCE_SUFFIXES)
    return sorted(files)


def harness_is_stale(binary: str | os.PathLike[str] = DEFAULT_BINARY) -> bool:
    """True if the binary is missing or older than any of its sources."""
    path = resolve_binary(binary)
    if not path.is_file():
        return True
    built = path.stat().st_mtime
    return any(src.stat().st_mtime > built for src in harness_sources())


def docker_unavailable_reason(timeout_s: float = 45.0, attempts: int = 2) -> str | None:
    """``None`` if a Docker daemon answers (Linux containers), else a human-readable reason.

    Docker Desktop occasionally stalls for a while, so a timeout is retried once."""
    if shutil.which("docker") is None:
        return "the docker CLI is not on PATH"
    reason = "docker did not answer"
    for _ in range(max(1, attempts)):
        try:
            proc = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}} {{.Server.Os}}"],
                capture_output=True, text=True, timeout=timeout_s, check=False,
            )
        except subprocess.TimeoutExpired:
            reason = f"'docker version' did not answer within {timeout_s:.0f} s"
            continue
        except OSError as exc:
            return f"cannot run docker: {exc}"
        if proc.returncode != 0:
            return f"the Docker daemon is not reachable: {(proc.stderr or proc.stdout).strip()[:300]}"
        if "linux" not in proc.stdout.lower():
            return f"Docker must run Linux containers (got {proc.stdout.strip()!r})"
        return None
    return reason


def build_harness(
    *,
    image: str | None = None,
    local: bool | None = None,
    timeout_s: float = 900.0,
    repo_root: Path = REPO_ROOT,
) -> Path:
    """Compile ``firmware/native/bin/harness``; returns its path.

    ``local=None`` uses the local g++ on Linux when available and Docker otherwise.
    Raises :class:`HarnessBuildError` with the compiler output on failure.
    """
    if local is None:
        local = sys.platform.startswith("linux") and shutil.which(os.environ.get("CXX", "g++")) is not None
    if local:
        cmd = ["sh", str(repo_root / BUILD_SCRIPT), "--local"]
    else:
        cmd = [
            "docker", "run", "--rm", "--network", "none", "-v", f"{repo_root}:{CONTAINER_ROOT}", "-w", CONTAINER_ROOT,
            image or os.environ.get("TACTIDOSE_GCC_IMAGE") or DEFAULT_IMAGE,
            "sh", f"{CONTAINER_ROOT}/{BUILD_SCRIPT}", "--in-container",
        ]
    log.info("building native harness: %s", subprocess.list2cmdline(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessBuildError(f"could not run the harness build ({exc})") from exc
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise HarnessBuildError(f"harness build failed (exit {proc.returncode}):\n{output.strip()}")
    log.debug("harness build output:\n%s", output)
    return repo_root / DEFAULT_BINARY


# --------------------------------------------------------------------------- target


class NativeTarget:
    """``ConformanceTarget`` backed by the native firmware harness (simulated time)."""

    name = "native"
    supports_faults = True
    supports_buttons = True
    supports_boot = True

    def __init__(
        self,
        binary: str | os.PathLike[str] = DEFAULT_BINARY,
        *,
        command: Sequence[str] | str | None = None,
        image: str | None = None,
        harness_args: Sequence[str] = (),
        speculate: bool = True,
        timeout_s: float = 30.0,
        startup_timeout_s: float = 120.0,
    ) -> None:
        """``command`` (or ``TACTIDOSE_NATIVE_HARNESS_CMD``) replaces the start command line;
        ``harness_args`` are appended to it (e.g. ``["--millis-offset", "4294900000"]``)."""
        self.binary = resolve_binary(binary)
        self._command_override: Sequence[str] | str | None = (
            command if command is not None else (os.environ.get(ENV_COMMAND) or None)
        )
        self._image = image or os.environ.get(ENV_IMAGE) or DEFAULT_IMAGE
        self._harness_args = [str(a) for a in harness_args]
        self.speculate = speculate
        self._timeout_s = timeout_s
        self._startup_timeout_s = startup_timeout_s
        if self._command_override is None and not self.binary.is_file():
            raise FileNotFoundError(
                f"native harness not found at {self.binary}. Build it first: "
                "'powershell -File firmware\\native\\build.ps1' (Windows), 'sh firmware/native/build.sh', "
                "or 'python -m tactidose.hardware.conformance_native build'."
            )
        self._proc: subprocess.Popen[bytes] | None = None
        self._container: str | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=40)
        self._started = False
        self._sim_ms = 0          # harness clock at the last !ack
        self._pending_ms = 0      # runner time not yet sent to the harness (known to be silent)
        self._silent_ms = 0       # remainder of the known-silent window after the runner's clock
        self._peek_ms = _PEEK_START_MS
        self._buffered: list[str] = []
        self.round_trips = 0

    # ------------------------------------------------------------------ ConformanceTarget

    def reset(self) -> None:
        self._buffered.clear()
        self._pending_ms = 0
        self._silent_ms = 0
        self._peek_ms = _PEEK_START_MS
        self._exchange(["!reset"])

    def boot(self, mode: str) -> None:
        if mode not in _BOOT_MODES:
            raise ValueError(f"boot mode must be one of {_BOOT_MODES}, got {mode!r}")
        self._input(f"!boot {mode}")

    def send(self, line: str) -> None:
        if "\n" in line or "\r" in line:
            raise ValueError("send() takes one line without terminators (use send_raw for raw bytes)")
        self._input(f"> {line}" if line else ">")

    def tick(self, ms: int) -> list[str]:
        ms = int(ms)
        if ms < 0:
            raise ValueError("tick() needs ms >= 0")
        if self.speculate and ms <= self._silent_ms:
            self._silent_ms -= ms
            self._pending_ms += ms
            return self._take_buffered()
        total, self._pending_ms = self._pending_ms + ms, 0
        self._run([f"!tick {total}"])
        return self._take_buffered()

    def set_button(self, name: str, pressed: bool) -> None:
        button = name.upper()
        if button not in _BUTTONS:
            raise ValueError(f"button must be one of {_BUTTONS}, got {name!r}")
        self._input(f"!button {button} {1 if pressed else 0}")

    def set_sensor(self, mode: str) -> None:
        if mode not in _SENSOR_MODES:
            raise ValueError(f"sensor mode must be one of {_SENSOR_MODES}, got {mode!r}")
        self._input(f"!sensor {mode}")

    def set_jam(self, on: bool) -> None:
        self._input(f"!jam {1 if on else 0}")

    def close(self) -> None:
        """Stop the harness (``!quit``); idempotent, never raises."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        clean = False
        try:
            if proc.poll() is None and proc.stdin is not None:
                try:
                    proc.stdin.write(b"!quit\n")
                    proc.stdin.flush()
                except OSError:
                    pass
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=15)
                clean = True
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log.warning("native harness process %s did not exit", proc.pid)
        except Exception:  # noqa: BLE001 - close() must not raise
            log.exception("error while stopping the native harness")
        finally:
            if not clean and self._container:
                try:
                    subprocess.run(["docker", "rm", "-f", self._container], capture_output=True, timeout=30,
                                   check=False)
                except (OSError, subprocess.TimeoutExpired):
                    log.warning("could not remove container %s", self._container)
            self._container = None

    # ------------------------------------------------------------------ extensions (tests, debugging)

    @property
    def now_ms(self) -> int:
        """Simulated time as seen by the caller (harness clock + locally accumulated ticks)."""
        return self._sim_ms + self._pending_ms

    def send_raw(self, data: bytes) -> None:
        """Deliver raw serial bytes (no implicit newline), e.g. ``b"PING\\r"``."""
        if not data:
            return
        self._input(f"!rx {data.hex()}")

    def physical(self) -> dict[str, str]:
        """Physics + safety-oracle report (``!physical``) at the caller's current time."""
        directives = self._flush_pending() + ["!physical"]
        device, info = self._exchange(directives)
        self._buffered.extend(device)
        for line in info:
            if line.startswith("!physical "):
                return dict(tok.split("=", 1) for tok in line.split()[1:] if "=" in tok)
        raise HarnessError("harness did not answer !physical")

    def parse_lines(self, texts: Sequence[bytes | str], num_slots: int | None = None) -> list[str]:
        """The firmware's ``parseLine()`` verdict for each text, e.g. ``"ok MOVE_SLOT 2"``,
        ``"err INVALID_SLOT MOVE_SLOT"`` or ``"empty"``. Does not touch the simulation."""
        suffix = "" if num_slots is None else f" {int(num_slots)}"
        directives = []
        for text in texts:
            raw = text.encode("latin-1") if isinstance(text, str) else bytes(text)
            directives.append(f"!parsehex {raw.hex() or '-'}{suffix}")  # "-" = zero bytes
        results: list[str] = []
        for start in range(0, len(directives), 500):
            _, info = self._exchange(directives[start:start + 500])
            results.extend(line[len("!parse "):] for line in info if line.startswith("!parse "))
        if len(results) != len(directives):
            raise HarnessError(f"expected {len(directives)} !parse replies, got {len(results)}")
        return results

    def __enter__(self) -> NativeTarget:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ internals

    def _take_buffered(self) -> list[str]:
        out, self._buffered = self._buffered, []
        return out

    def _flush_pending(self) -> list[str]:
        if not self._pending_ms:
            return []
        directive, self._pending_ms = f"!tick {self._pending_ms}", 0
        return [directive]

    def _input(self, directive: str) -> None:
        # Inputs come in bursts (the runner sends the next one as soon as its lines arrived), so
        # peek a short window; it only grows while the caller keeps ticking without input.
        self._peek_ms = _PEEK_START_MS
        self._run(self._flush_pending() + [directive])

    def _run(self, directives: list[str]) -> None:
        """Exchange ``directives`` (+ ``!peek`` when speculating); buffer device lines."""
        if self.speculate:
            directives = [*directives, f"!peek {self._peek_ms}"]
        device, info = self._exchange(directives)
        self._buffered.extend(device)
        if not self.speculate:
            return
        silent = next((int(line.split()[1]) for line in info if line.startswith("!peek ")), None)
        if silent is None:
            raise HarnessError("harness did not answer !peek")
        self._silent_ms = silent
        self._peek_ms = min(self._peek_ms * 2, _PEEK_MAX_MS) if silent >= self._peek_ms else _PEEK_START_MS

    def _exchange(self, directives: Sequence[str]) -> tuple[list[str], list[str]]:
        """Send directives, wait for one ``!ack`` each. Returns (device lines, harness lines)."""
        proc = self._ensure_started()
        assert proc.stdin is not None
        payload = "".join(d + "\n" for d in directives).encode("latin-1")
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except OSError as exc:
            raise HarnessError(f"cannot write to the native harness: {exc}{self._stderr_tail()}") from exc
        self.round_trips += 1
        timeout = self._timeout_s if self._started else self._startup_timeout_s
        deadline = time.monotonic() + timeout
        device: list[str] = []
        info: list[str] = []
        errors: list[str] = []
        acks = 0
        while acks < len(directives):
            try:
                line = self._lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise HarnessError(
                    f"native harness did not acknowledge {list(directives)!r} within {timeout:.0f} s"
                    f"{self._stderr_tail()}"
                ) from None
            if line is None:
                raise HarnessError(f"native harness exited (code {proc.poll()}){self._stderr_tail()}")
            if line.startswith("!ack"):
                acks += 1
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    self._sim_ms = int(parts[1])
            elif line.startswith("!err"):
                errors.append(line)
            elif line.startswith("!"):
                info.append(line)
            else:
                device.append(line)
        self._started = True
        if errors:
            raise HarnessError(f"native harness rejected input: {errors}")
        return device, info

    def _ensure_started(self) -> subprocess.Popen[bytes]:
        if self._proc is not None:
            if self._proc.poll() is None:
                return self._proc
            raise HarnessError(f"native harness exited (code {self._proc.returncode}){self._stderr_tail()}")
        cmd = self._command()
        shown = cmd if isinstance(cmd, str) else subprocess.list2cmdline(cmd)
        log.info("starting native firmware harness: %s", shown)
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
            )
        except OSError as exc:
            hint = " (is Docker installed and running?)" if "docker" in shown else ""
            raise HarnessError(f"cannot start the native harness{hint}: {exc}") from exc
        self._proc = proc
        self._lines = queue.Queue()
        self._started = False
        assert proc.stdout is not None and proc.stderr is not None
        threading.Thread(target=self._pump_stdout, args=(proc.stdout, self._lines),
                         name="native-harness-stdout", daemon=True).start()
        threading.Thread(target=self._pump_stderr, args=(proc.stderr,),
                         name="native-harness-stderr", daemon=True).start()
        return proc

    def _command(self) -> list[str] | str:
        args = self._harness_args
        override = self._command_override
        if override is not None:
            if isinstance(override, str):
                if os.name == "nt":  # CreateProcess parses the string, exactly as typed in a console
                    return " ".join([override, *(subprocess.list2cmdline([a]) for a in args)])
                return [*shlex.split(override), *args]
            return [*override, *args]
        if sys.platform.startswith("linux"):
            return [str(self.binary), *args]
        return self._docker_command(args)

    def _docker_command(self, args: Sequence[str]) -> list[str]:
        self._container = f"tactidose-harness-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        cmd = ["docker", "run", "-i", "--rm", "--name", self._container, "--network", "none"]
        binary = self.binary.resolve()
        try:
            rel = binary.relative_to(REPO_ROOT.resolve())
            cmd += ["-v", f"{REPO_ROOT}:{CONTAINER_ROOT}:ro", "-w", CONTAINER_ROOT, self._image,
                    f"{CONTAINER_ROOT}/{rel.as_posix()}"]
        except ValueError:  # binary outside the repository: mount its folder instead
            cmd += ["-v", f"{binary.parent}:/harness:ro", "-w", "/harness", self._image, f"/harness/{binary.name}"]
        return [*cmd, *args]

    @staticmethod
    def _pump_stdout(stream: IO[bytes], lines: queue.Queue[str | None]) -> None:
        try:
            for raw in iter(stream.readline, b""):
                lines.put(raw.decode("utf-8", "replace").rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    def _pump_stderr(self, stream: IO[bytes]) -> None:
        try:
            for raw in iter(stream.readline, b""):
                text = raw.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text)
                    log.debug("harness stderr: %s", text)
        except (OSError, ValueError):
            pass

    def _stderr_tail(self) -> str:
        return ("\n  harness stderr: " + " | ".join(self._stderr)) if self._stderr else ""


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tactidose.hardware.conformance_native",
        description="Build / inspect the native firmware conformance harness (firmware/native).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="compile firmware/native/bin/harness (Docker gcc:14 unless on Linux)")
    b.add_argument("--image", default=None, help=f"gcc Docker image (default {DEFAULT_IMAGE})")
    b.add_argument("--if-stale", action="store_true", help="only build if sources are newer than the binary")
    b.add_argument("--docker", action="store_true", help="build in Docker even on Linux")
    sub.add_parser("status", help="show binary path, staleness and Docker availability")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    binary = resolve_binary(DEFAULT_BINARY)
    if args.cmd == "status":
        reason = docker_unavailable_reason()
        print(f"binary : {binary} ({'missing' if not binary.is_file() else 'present'})")
        print(f"stale  : {harness_is_stale(binary)}")
        print(f"docker : {'ok' if reason is None else reason}")
        return 0
    if args.if_stale and not harness_is_stale(binary):
        print(f"{binary} is up to date")
        return 0
    try:
        path = build_harness(image=args.image, local=False if args.docker else None)
    except HarnessBuildError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"built {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

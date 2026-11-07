"""PipeWire microphone capture with explicit start/stop and bounded shutdown."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import BinaryIO
import wave

from .core import Library, Memo


@dataclass
class _Capture:
    process: subprocess.Popen
    path: Path
    errors: BinaryIO
    started: float
    interrupted: bool = False


class Recorder:
    def __init__(self, library: Library):
        self.library = library
        self._capture: _Capture | None = None
        self._saving = False
        self._lock = threading.RLock()

    @property
    def is_recording(self) -> bool:
        with self._lock:
            # poll() owns failure reporting; do not silently lose an exited session.
            return self._capture is not None

    @property
    def elapsed(self) -> float:
        with self._lock:
            capture = self._capture
            return max(0.0, time.monotonic() - capture.started) if capture else 0.0

    @staticmethod
    def _detail(capture: _Capture) -> str:
        capture.errors.seek(0, os.SEEK_END)
        capture.errors.seek(max(0, capture.errors.tell() - 4096))
        return capture.errors.read().decode("utf-8", errors="replace").strip()

    @staticmethod
    def _cleanup(capture: _Capture, *, preserve: bool = False) -> None:
        capture.errors.close()
        if not preserve:
            shutil.rmtree(capture.path.parent, ignore_errors=True)

    @staticmethod
    def _has_audio(capture: _Capture) -> bool:
        try:
            return capture.path.stat().st_size > 44
        except FileNotFoundError:
            return False

    @staticmethod
    def _capture_is_flowing(capture: _Capture) -> bool:
        # WAV headers can contain additional metadata chunks. At 48 kHz even
        # silent PCM is far larger than 4 KiB after the five-second grace period.
        try:
            return capture.path.stat().st_size > 4096
        except FileNotFoundError:
            return False

    @staticmethod
    def _validate_recording(capture: _Capture) -> None:
        """Require finalized, nonempty PCM without loading a recording in RAM."""
        try:
            with capture.path.open("rb") as source:
                header = source.read(12)
                if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                    raise ValueError("the file is not a valid WAV recording")
                if int.from_bytes(header[4:8], "little") + 8 > capture.path.stat().st_size:
                    raise ValueError("the WAV file is truncated or was not finalized")
            with wave.open(str(capture.path), "rb") as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 48000):
                    raise ValueError("expected mono, 16-bit, 48 kHz PCM audio")
                frames = audio.getnframes()
                if frames == 0:
                    raise ValueError("the recording has no audio frames; check the default microphone")
                audio.setpos(frames - 1)
                if len(audio.readframes(1)) != 2:
                    raise ValueError("the WAV audio samples are incomplete")
        except (OSError, EOFError, ValueError, wave.Error) as exc:
            detail = str(exc) or "the WAV header is incomplete"
            raise RuntimeError(f"PipeWire produced an unusable recording (exit code {capture.process.returncode}): {detail}.") from exc

    def start(self) -> None:
        with self._lock:
            if self._capture is not None or self._saving:
                raise RuntimeError("A recording is already in progress or being saved.")
            if not sys.platform.startswith("linux"):
                raise RuntimeError("Microphone recording requires Linux with PipeWire and pw-record.")
            executable = shutil.which("pw-record")
            if executable is None:
                raise RuntimeError("pw-record was not found. Install pipewire-audio and start your PipeWire session.")
            recovery = self.library.root / "recovery"
            recovery.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="recording-", dir=recovery))
            path = directory / "recording.wav"
            errors = tempfile.TemporaryFile()
            try:
                process = subprocess.Popen(
                    [executable, "--format", "s16", "--rate", "48000", "--channels", "1", str(path)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                )
            except OSError as exc:
                errors.close()
                shutil.rmtree(directory, ignore_errors=True)
                raise RuntimeError(f"Could not start pw-record: {exc}") from exc
            capture = _Capture(process, path, errors, time.monotonic())
            try:
                process.wait(timeout=0.15)
            except subprocess.TimeoutExpired:
                self._capture = capture
                return
            detail = self._detail(capture)
            self._cleanup(capture)
            raise RuntimeError("Could not start microphone recording. Check the PipeWire server and default microphone."
                               + (f"\n{detail}" if detail else ""))

    @staticmethod
    def _halt(capture: _Capture) -> bool:
        """Return True when the process needed forced termination."""
        process = capture.process
        if process.poll() is not None:
            return False
        try:
            process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return False
        # Popen.send_signal may notice an already exited child and skip sending
        # the signal. Only accept PipeWire's special exit status when we sent it
        # to an active child, rather than when the process exited on its own.
        capture.interrupted = process.returncode is None
        try:
            process.wait(timeout=5)
            return False
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
            except ProcessLookupError:
                return False
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
        return True

    def _release_failed(self, capture: _Capture, preserve: bool) -> None:
        try:
            self._halt(capture)
        finally:
            self._cleanup(capture, preserve=preserve)

    def poll(self) -> str | None:
        """Report capture errors once without waiting for process shutdown.

        A missing source may leave pw-record waiting. No PCM after five seconds
        is a setup failure; ordinary silence still produces PCM. A daemon worker
        releases a failed process, keeping GUI timer calls responsive.
        """
        with self._lock:
            capture = self._capture
            if capture is None:
                return None
            exited = capture.process.poll() is not None
            no_audio = time.monotonic() - capture.started > 5 and not self._capture_is_flowing(capture)
            if not exited and not no_audio:
                return None
            self._capture = None
        detail = self._detail(capture)
        message = ("No microphone audio arrived. Check your default input device and PipeWire session."
                   if no_audio else "Microphone recording stopped unexpectedly. Check the input device and PipeWire session.")
        if detail:
            message += f"\n{detail}"
        preserve = self._has_audio(capture)
        if preserve:
            message += f"\nCaptured audio was kept at {capture.path}"
        threading.Thread(target=self._release_failed, args=(capture, preserve), daemon=True,
                         name="omamemo-release-capture").start()
        return message

    def stop(self) -> Memo:
        # Only state changes hold the lock. Neither process waits nor audio
        # conversion can block GUI reads of elapsed/is_recording/poll.
        with self._lock:
            capture = self._capture
            if capture is None:
                raise RuntimeError("There is no recording to stop.")
            self._capture = None
            self._saving = True
        preserve = False
        try:
            if capture.process.poll() is not None:
                detail = self._detail(capture)
                raise RuntimeError("The microphone stopped before the recording was saved."
                                   + (f"\n{detail}" if detail else ""))
            if self._halt(capture):
                raise RuntimeError("The microphone did not stop cleanly; the audio may be incomplete.")
            code = capture.process.returncode
            detail = self._detail(capture)
            # pw-record's SIGINT handler quits its loop; unlike playback drain,
            # that path can retain EXIT_FAILURE even after sf_close finalizes a
            # healthy WAV. Accept only that quiet, intentional graceful exit,
            # followed by structural validation and the normal decoder import.
            pipewire_interrupted = code == 1 and capture.interrupted and not detail
            if code not in (0, -signal.SIGINT, 128 + signal.SIGINT) and not pipewire_interrupted:
                raise RuntimeError(f"PipeWire could not finish the recording (exit code {code})."
                                   + (f"\n{detail}" if detail else ""))
            self._validate_recording(capture)
            return self.library.import_audio(capture.path, title=datetime.now().strftime("Recording · %d %b %Y, %H:%M"))
        except BaseException as exc:
            preserve = self._has_audio(capture)
            if isinstance(exc, Exception):
                recovery = f"\nCaptured audio was kept at {capture.path}" if preserve else ""
                raise RuntimeError(f"Could not save recording: {exc}{recovery}") from exc
            raise
        finally:
            self._cleanup(capture, preserve=preserve)
            with self._lock:
                self._saving = False

    def cancel(self) -> None:
        with self._lock:
            capture = self._capture
            self._capture = None
        if capture is not None:
            try:
                self._halt(capture)
            finally:
                self._cleanup(capture)

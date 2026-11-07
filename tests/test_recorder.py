from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

from omamemo.core import Library, Memo
from omamemo.recorder import Recorder


class FakeProcess:
    def __init__(self, args, *, stderr, early=False, stubborn=False, empty=False,
                 stop_returncode=0, stop_diagnostic="", **kwargs):
        self.args = args
        self.returncode = 1 if early else None
        self.stubborn = stubborn
        self.stop_returncode = stop_returncode
        self.stop_diagnostic = stop_diagnostic
        self.stderr = stderr
        self.stop_requested = False
        self.signals = []
        self.killed = False
        self.path = Path(args[-1])
        if not empty:
            with wave.open(str(self.path), "wb") as audio:
                audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\0\0" * 4800)
        if early:
            stderr.write(b"PipeWire server is not running")
            stderr.flush()

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        if self.stop_requested and not self.stubborn:
            self.returncode = self.stop_returncode
            if self.stop_diagnostic:
                self.stderr.write(self.stop_diagnostic.encode())
                self.stderr.flush()
        if self.returncode is None:
            raise subprocess.TimeoutExpired("pw-record", timeout)
        return self.returncode

    def send_signal(self, value):
        self.signals.append(value)
        self.stop_requested = True

    def terminate(self):
        if not self.stubborn:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.library = Mock()
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.library.root = Path(self.scratch.name)
        self.library.import_audio.return_value = Memo("new", "Recorded", "now", .1, Path("saved.wav"))
        self.recorder = Recorder(self.library)
        self.addCleanup(self.recorder.cancel)
        self.addCleanup(patch.stopall)
        patch("omamemo.recorder.sys.platform", "linux").start()
        patch("omamemo.recorder.shutil.which", return_value="/usr/bin/pw-record").start()

    def start(self, **options):
        factory = lambda args, **kwargs: FakeProcess(args, **kwargs, **options)
        with patch("omamemo.recorder.subprocess.Popen", side_effect=factory):
            self.recorder.start()
        return self.recorder._capture.process

    def test_stop_gracefully_imports_and_cleans_scratch(self):
        process = self.start()
        folder = process.path.parent
        self.assertTrue(self.recorder.is_recording)
        self.assertGreaterEqual(self.recorder.elapsed, 0)
        self.assertIsNone(self.recorder.poll())
        result = self.recorder.stop()
        self.assertEqual(result.id, "new")
        self.assertIn(signal.SIGINT, process.signals)
        self.library.import_audio.assert_called_once()
        self.assertFalse(folder.exists())
        self.assertFalse(self.recorder.is_recording)
        self.assertEqual(self.recorder.elapsed, 0)
        self.assertEqual(process.args[1:7], ["--format", "s16", "--rate", "48000", "--channels", "1"])

    def test_cancel_discards_capture(self):
        process = self.start()
        self.recorder.cancel()
        self.assertFalse(process.path.exists())
        self.library.import_audio.assert_not_called()

    def test_requires_linux_and_pipewire_tool(self):
        with patch("omamemo.recorder.sys.platform", "darwin"), self.assertRaisesRegex(RuntimeError, "requires Linux"):
            self.recorder.start()
        with patch("omamemo.recorder.shutil.which", return_value=None), self.assertRaisesRegex(RuntimeError, "not found"):
            self.recorder.start()

    def test_early_server_failure_has_helpful_diagnostics(self):
        with self.assertRaisesRegex(RuntimeError, "PipeWire server is not running"):
            self.start(early=True)
        self.assertFalse(self.recorder.is_recording)

    def test_rejects_double_start_and_empty_stop(self):
        with self.assertRaises(RuntimeError):
            self.recorder.stop()
        self.start()
        with self.assertRaisesRegex(RuntimeError, "already"):
            self.recorder.start()

    def test_unexpected_exit_is_reported_once_and_preserves_audio(self):
        process = self.start()
        self.addCleanup(shutil.rmtree, process.path.parent, True)
        process.returncode = 2
        with patch("omamemo.recorder.threading.Thread") as worker:
            self.assertIn("stopped unexpectedly", self.recorder.poll())
            worker.return_value.start.assert_called_once()
        self.recorder._release_failed(*worker.call_args.kwargs["args"])
        self.assertTrue(process.path.exists())
        self.assertIsNone(self.recorder.poll())
        self.assertFalse(self.recorder.is_recording)

    def test_missing_microphone_times_out_without_import(self):
        process = self.start(empty=True)
        with patch("omamemo.recorder.time.monotonic", return_value=self.recorder._capture.started + 6), \
             patch("omamemo.recorder.threading.Thread") as worker:
            self.assertIn("No microphone audio", self.recorder.poll())
        self.recorder._release_failed(*worker.call_args.kwargs["args"])
        self.assertFalse(process.path.parent.exists())
        self.library.import_audio.assert_not_called()

    def test_hung_process_is_killed_with_bounded_waits(self):
        process = self.start(stubborn=True)
        self.addCleanup(shutil.rmtree, process.path.parent, True)
        with self.assertRaisesRegex(RuntimeError, "did not stop cleanly"):
            self.recorder.stop()
        self.assertTrue(process.killed)
        self.assertTrue(process.path.exists())
        self.library.import_audio.assert_not_called()

    def test_failed_import_keeps_recovery_file(self):
        process = self.start()
        self.addCleanup(shutil.rmtree, process.path.parent, True)
        self.library.import_audio.side_effect = RuntimeError("disk full")
        with self.assertRaisesRegex(RuntimeError, "Captured audio was kept at"):
            self.recorder.stop()
        self.assertTrue(process.path.exists())
        self.assertFalse(self.recorder.is_recording)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_pipewire_sigint_exit_one_saves_valid_wav_in_real_library(self):
        self.library = Library(self.library.root / "real-library")
        self.recorder = Recorder(self.library)
        self.addCleanup(self.recorder.cancel)
        process = self.start(stop_returncode=1)
        memo = self.recorder.stop()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.signals, [signal.SIGINT])
        self.assertAlmostEqual(memo.duration, .1, places=3)
        self.assertEqual(self.library.get(memo.id), memo)
        with wave.open(str(memo.audio_path), "rb") as audio:
            self.assertEqual(audio.getnframes(), 4800)
        self.assertFalse(process.path.parent.exists())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_exit_one_retains_capture_when_real_import_database_write_fails(self):
        self.library = Library(self.library.root / "real-library")
        self.recorder = Recorder(self.library)
        self.addCleanup(self.recorder.cancel)
        process = self.start(stop_returncode=1)
        original = process.path.read_bytes()
        with patch.object(self.library, "_connect", side_effect=sqlite3.OperationalError("disk full")), \
             self.assertRaisesRegex(RuntimeError, "disk full.*"):
            self.recorder.stop()
        self.assertEqual(process.path.read_bytes(), original)
        self.assertEqual(self.library.list_memos(), [])
        self.assertEqual(list(self.library.audio_dir.iterdir()), [])

    def test_exit_one_rejects_corrupt_or_truncated_wav_and_keeps_recovery(self):
        for corrupted in (b"not a WAV file" * 10, None):
            with self.subTest(corrupted=corrupted is not None):
                process = self.start(stop_returncode=1)
                contents = corrupted if corrupted is not None else process.path.read_bytes()[:-64]
                process.path.write_bytes(contents)
                with self.assertRaisesRegex(RuntimeError, "unusable recording \\(exit code 1\\)"):
                    self.recorder.stop()
                self.assertEqual(process.path.read_bytes(), contents)
                self.library.import_audio.assert_not_called()

    def test_exit_one_rejects_wav_with_no_frames(self):
        process = self.start(stop_returncode=1)
        with wave.open(str(process.path), "wb") as audio:
            audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            audio.writeframes(b"")
        with self.assertRaisesRegex(RuntimeError, "exit code 1.*no audio frames"):
            self.recorder.stop()
        self.library.import_audio.assert_not_called()

    def test_exit_one_with_error_diagnostic_is_not_treated_as_normal_stop(self):
        process = self.start(stop_returncode=1, stop_diagnostic="write error: No space left on device")
        with self.assertRaisesRegex(RuntimeError, "exit code 1") as result:
            self.recorder.stop()
        self.assertIn("No space left on device", str(result.exception))
        self.assertTrue(process.path.exists())
        self.library.import_audio.assert_not_called()

    def test_unrecognized_exit_keeps_code_in_diagnostic(self):
        self.start(stop_returncode=7)
        with self.assertRaisesRegex(RuntimeError, "exit code 7"):
            self.recorder.stop()
        self.library.import_audio.assert_not_called()

    def test_exit_one_before_our_stop_is_still_an_unexpected_exit(self):
        process = self.start(stop_returncode=1)
        process.returncode = 1
        with self.assertRaisesRegex(RuntimeError, "microphone stopped before"):
            self.recorder.stop()
        self.assertEqual(process.signals, [])
        self.assertTrue(process.path.exists())
        self.library.import_audio.assert_not_called()

    def test_state_reads_do_not_block_during_slow_save(self):
        self.start()
        entered = threading.Event()
        release = threading.Event()

        def import_audio(*args, **kwargs):
            entered.set()
            release.wait(timeout=3)
            return Memo("saved", "Saved", "now", .1, Path("saved.wav"))

        self.library.import_audio.side_effect = import_audio
        worker = threading.Thread(target=self.recorder.stop)
        worker.start()
        self.assertTrue(entered.wait(timeout=1))
        with ThreadPoolExecutor(max_workers=1) as readers:
            try:
                snapshot = readers.submit(lambda: (self.recorder.is_recording, self.recorder.elapsed, self.recorder.poll()))
                self.assertEqual(snapshot.result(timeout=.25), (False, 0, None))
            finally:
                release.set()
                worker.join(timeout=3)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()

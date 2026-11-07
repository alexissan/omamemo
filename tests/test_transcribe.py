from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from omamemo.core import Memo, _run_command
from omamemo.transcribe import is_available, transcribe


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name)
        self.model = self.folder / "a local model.bin"
        self.model.write_bytes(b"fixture-model")
        self.audio = self.folder / "memo.wav"
        self.audio.write_bytes(b"fixture-audio")
        self.memo = Memo("memo", "A note", "now", 10, self.audio)

    def test_runs_local_conversion_and_reads_only_transcript_file(self):
        commands = []
        scratch_paths = []

        def execute(args, **kwargs):
            commands.append((args, kwargs))
            if args[0] == "/usr/bin/whisper-cli":
                path = Path(args[args.index("-of") + 1]).with_suffix(".txt")
                scratch_paths.append(path)
                path.write_text("  This is the transcript.\n", encoding="utf-8")
            return "diagnostics that are not part of the transcript"

        with patch("omamemo.transcribe.shutil.which", return_value="/usr/bin/whisper-cli"), \
             patch("omamemo.transcribe._run_command", side_effect=execute):
            self.assertEqual(transcribe(self.memo, self.model), "This is the transcript.")
        ffmpeg, whisper = commands
        self.assertIn("16000", ffmpeg[0])
        self.assertIn("pcm_s16le", ffmpeg[0])
        self.assertEqual(whisper[0][whisper[0].index("-m") + 1], str(self.model.resolve()))
        self.assertIn("auto", whisper[0])
        self.assertGreater(whisper[1]["timeout"], 0)
        self.assertFalse(scratch_paths[0].exists())
        self.assertEqual(self.audio.read_bytes(), b"fixture-audio")

    def test_model_and_executable_must_already_exist(self):
        with self.assertRaises(FileNotFoundError):
            transcribe(self.memo, self.folder / "missing.bin")
        with patch("omamemo.transcribe.shutil.which", return_value=None):
            self.assertFalse(is_available())
            with self.assertRaisesRegex(RuntimeError, "whisper-cli was not found"):
                transcribe(self.memo, self.model)

    def test_missing_output_is_error_and_scratch_is_removed(self):
        commands = []

        def execute(args, **kwargs):
            commands.append(args)
            return ""

        with patch("omamemo.transcribe.shutil.which", return_value="/usr/bin/whisper-cli"), \
             patch("omamemo.transcribe._run_command", side_effect=execute), \
             self.assertRaisesRegex(RuntimeError, "without writing a transcript"):
            transcribe(self.memo, self.model)
        output = Path(commands[1][commands[1].index("-of") + 1])
        self.assertFalse(output.parent.exists())

    def test_cli_failure_does_not_modify_original(self):
        with patch("omamemo.transcribe.shutil.which", return_value="/usr/bin/whisper-cli"), \
             patch("omamemo.transcribe._run_command", side_effect=RuntimeError("bad model")), \
             self.assertRaisesRegex(RuntimeError, "bad model"):
            transcribe(self.memo, self.model)
        self.assertEqual(self.audio.read_bytes(), b"fixture-audio")

    def test_command_wrapper_reports_missing_tool_and_timeout(self):
        with patch("omamemo.core.subprocess.run", side_effect=FileNotFoundError()), \
             self.assertRaisesRegex(RuntimeError, "not installed"):
            _run_command(["missing-tool"])
        with patch("omamemo.core.subprocess.run", side_effect=subprocess.TimeoutExpired("tool", 1)), \
             self.assertRaisesRegex(RuntimeError, "timed out"):
            _run_command(["tool"], timeout=1)


if __name__ == "__main__":
    unittest.main()

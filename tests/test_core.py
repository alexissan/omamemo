from array import array
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import math
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import wave

from omamemo.core import Library


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name)
        self.source = self.folder / "source ' voice ; $(ignored).wav"
        rate = 22050
        samples = array("h")
        for frame in range(rate):
            value = int(15000 * math.sin(2 * math.pi * 440 * frame / rate))
            samples.extend((value, value))
        with wave.open(str(self.source), "wb") as audio:
            audio.setparams((2, 2, rate, 0, "NONE", "not compressed"))
            audio.writeframes(samples.tobytes())
        self.library = Library(self.folder / "library")

    def test_import_normalizes_and_has_actual_peaks(self):
        memo = self.library.import_audio(self.source, "A note")
        self.assertAlmostEqual(memo.duration, 1.0, places=3)
        with wave.open(str(memo.audio_path)) as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 48000))
        self.assertEqual(len(memo.peaks), 160)
        self.assertGreater(min(memo.peaks), 0.4)
        self.assertLess(max(memo.peaks), 0.6)
        self.assertEqual(self.library.get(memo.id), memo)
        self.assertTrue(self.source.exists())
        self.assertEqual(len(self.library.list_memos()), 1)

    def test_rename_transcript_search_and_newest_first(self):
        first = self.library.import_audio(self.source, "Morning")
        second = self.library.import_audio(self.source, "Later")
        self.library.rename(first.id, "  MÉMO  ")
        self.library.set_transcript(first.id, "A thoughtful meeting about Orcas")
        self.assertEqual([m.id for m in self.library.list_memos()], [second.id, first.id])
        self.assertEqual(self.library.list_memos("mémo")[0].id, first.id)
        self.assertEqual(self.library.list_memos("ORCAS")[0].id, first.id)
        self.assertEqual(self.library.get(first.id).title, "MÉMO")
        self.assertEqual(self.library.list_memos("no match"), [])
        with self.assertRaises(ValueError):
            self.library.rename(first.id, " ")
        with self.assertRaises(KeyError):
            self.library.get("missing")

    def test_trim_is_new_copy_and_keeps_original(self):
        original = self.library.import_audio(self.source, "Original")
        self.library.set_transcript(original.id, "whole original")
        original_bytes = original.audio_path.read_bytes()
        trimmed = self.library.trim(original.id, 0.2, 0.7)
        self.assertNotEqual(trimmed.id, original.id)
        self.assertAlmostEqual(trimmed.duration, 0.5, places=3)
        self.assertEqual(original.audio_path.read_bytes(), original_bytes)
        self.assertEqual(self.library.get(original.id).transcript, "whole original")
        self.assertEqual(trimmed.transcript, "")
        for start, end in ((0, 0), (-1, .5), (.5, .4), (0, 2), (float("nan"), .5)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.library.trim(original.id, start, end)

    def test_failed_import_and_trim_leave_no_partial_audio(self):
        original = self.library.import_audio(self.source)
        before = set(self.library.audio_dir.iterdir())
        with patch("omamemo.core._run_command", side_effect=RuntimeError("decoder failed")):
            with self.assertRaisesRegex(RuntimeError, "decoder failed"):
                self.library.import_audio(self.source)
            with self.assertRaises(RuntimeError):
                self.library.trim(original.id, 0, .5)
        self.assertEqual(set(self.library.audio_dir.iterdir()), before)
        self.assertEqual(len(self.library.list_memos()), 1)
        invalid = self.folder / "bad.mp3"
        invalid.write_text("this is not audio")
        with self.assertRaises(RuntimeError):
            self.library.import_audio(invalid)
        self.assertEqual(set(self.library.audio_dir.iterdir()), before)

    def test_failed_database_write_removes_converted_audio(self):
        with patch.object(self.library, "_connect", side_effect=sqlite3.OperationalError("database failed")):
            with self.assertRaises(sqlite3.OperationalError):
                self.library.import_audio(self.source)
        self.assertEqual(list(self.library.audio_dir.iterdir()), [])
        self.assertEqual(self.library.list_memos(), [])

    def test_exports_real_wav_and_mp3_and_refuses_overwrites(self):
        memo = self.library.import_audio(self.source)
        exported = self.folder / "export.wav"
        self.library.export(memo.id, exported)
        self.assertEqual(exported.read_bytes(), memo.audio_path.read_bytes())
        with self.assertRaises(FileExistsError):
            self.library.export(memo.id, exported)
        with self.assertRaises(ValueError):
            self.library.export(memo.id, memo.audio_path)
        with self.assertRaises(ValueError):
            self.library.export(memo.id, self.folder / "unsupported.flac")
        mp3 = self.folder / "export.mp3"
        self.library.export(memo.id, mp3)
        decoded = self.library.import_audio(mp3)
        self.assertAlmostEqual(decoded.duration, 1, delta=.1)

    def test_failed_export_does_not_publish_file(self):
        memo = self.library.import_audio(self.source)
        destination = self.folder / "export.mp3"
        with patch("omamemo.core._run_command", side_effect=RuntimeError("encoder failed")):
            with self.assertRaises(RuntimeError):
                self.library.export(memo.id, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.folder.glob(".omamemo-export-*")), [])

    def test_delete_retains_audio_and_metadata_in_trash(self):
        memo = self.library.import_audio(self.source)
        self.library.set_transcript(memo.id, "keep this in trash")
        self.library.delete(memo.id)
        self.assertEqual(self.library.list_memos(), [])
        self.assertFalse(memo.audio_path.exists())
        self.assertTrue((self.library.trash_dir / f"{memo.id}.wav").exists())
        with closing(sqlite3.connect(self.library.db_path)) as db:
            row = db.execute("SELECT transcript,deleted_at FROM memos WHERE id=?", (memo.id,)).fetchone()
        self.assertEqual(row[0], "keep this in trash")
        self.assertTrue(row[1])
        with self.assertRaises(KeyError):
            self.library.get(memo.id)

    def test_one_library_can_be_used_from_worker_threads(self):
        memo = self.library.import_audio(self.source)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(self.library.set_transcript, memo.id, f"text {i}") for i in range(6)]
            for future in futures:
                self.assertEqual(future.result().id, memo.id)
        self.assertTrue(self.library.get(memo.id).transcript.startswith("text "))


if __name__ == "__main__":
    unittest.main()

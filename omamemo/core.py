"""Durable, local audio library. No GUI or third-party Python dependencies."""

from __future__ import annotations

from array import array
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
import wave


@dataclass
class Memo:
    id: str
    title: str
    created_at: str
    duration: float
    audio_path: Path
    transcript: str = ""
    peaks: list[float] = field(default_factory=list)


def _run_command(args: list[str], *, timeout: float = 600,
                 capture_stdout: bool = False) -> str:
    """Bound execution and keep potentially lengthy decoder diagnostics on disk."""
    with tempfile.TemporaryFile() as diagnostics:
        try:
            result = subprocess.run(
                args, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
                stderr=diagnostics, text=True, timeout=timeout, check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"{Path(args[0]).name} is not installed or is not on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{Path(args[0]).name} timed out after {timeout:g} seconds.") from exc
        except OSError as exc:
            raise RuntimeError(f"Could not run {Path(args[0]).name}: {exc}") from exc
        if result.returncode:
            diagnostics.seek(0, os.SEEK_END)
            diagnostics.seek(max(0, diagnostics.tell() - 8192))
            detail = diagnostics.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"{Path(args[0]).name} failed: {detail or 'exit code ' + str(result.returncode)}")
        return result.stdout or ""


def _peaks(path: Path, count: int = 160) -> list[float]:
    """Read PCM in bounded chunks, retaining the absolute peak of each time bin."""
    peaks: list[float] = []
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2 or audio.getnchannels() != 1:
            raise ValueError("Expected mono 16-bit PCM audio.")
        total = audio.getnframes()
        if total == 0:
            raise ValueError("The file contains no audio samples.")
        bins = min(count, total)
        consumed = 0
        for index in range(bins):
            boundary = (index + 1) * total // bins
            peak = 0
            while consumed < boundary:
                frames = min(8192, boundary - consumed)
                samples = array("h", audio.readframes(frames))
                if not samples:
                    raise ValueError("The audio file ended unexpectedly.")
                if sys.byteorder != "little":
                    samples.byteswap()
                peak = max(peak, abs(min(samples)), abs(max(samples)))
                consumed += len(samples)
            peaks.append(round(peak / 32768, 5))
    return peaks


class Library:
    """SQLite metadata with managed PCM files and a recoverable local trash.

    Every operation opens its own SQLite connection so worker threads can share
    one Library instance. Imported/trimmed files become visible only after a
    successful conversion, validation, and metadata transaction.
    """

    def __init__(self, root: Path | None = None):
        if root is None:
            data_home = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
            if not data_home.is_absolute():
                data_home = Path.home() / ".local/share"
            root = data_home / "omamemo"
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.audio_dir = self.root / "audio"
        self.trash_dir = self.root / "trash"
        self.audio_dir.mkdir(mode=0o700, exist_ok=True)
        self.trash_dir.mkdir(mode=0o700, exist_ok=True)
        self.db_path = self.root / "memos.sqlite3"
        with closing(self._connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS memos (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
                duration REAL NOT NULL, audio_path TEXT NOT NULL,
                transcript TEXT NOT NULL DEFAULT '', peaks TEXT NOT NULL,
                deleted_at TEXT
            )""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _memo(self, row: sqlite3.Row) -> Memo:
        return Memo(row["id"], row["title"], row["created_at"], row["duration"],
                    self.root / row["audio_path"], row["transcript"], json.loads(row["peaks"]))

    def list_memos(self, query: str = "") -> list[Memo]:
        # Python casefold handles non-ASCII titles; SQLite's default LIKE does not.
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM memos WHERE deleted_at IS NULL ORDER BY created_at DESC, id DESC").fetchall()
        needle = query.strip().casefold()
        return [self._memo(row) for row in rows
                if not needle or needle in row["title"].casefold() or needle in row["transcript"].casefold()]

    def get(self, id: str) -> Memo:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM memos WHERE id=? AND deleted_at IS NULL", (id,)).fetchone()
        if row is None:
            raise KeyError(f"Memo {id!r} was not found.")
        return self._memo(row)

    @staticmethod
    def _title(title: str) -> str:
        title = title.strip()
        if not title:
            raise ValueError("Give the memo a title.")
        if len(title) > 500:
            raise ValueError("Titles must be 500 characters or fewer.")
        return title

    def _create(self, source: Path, title: str, *, start: float | None = None,
                end: float | None = None) -> Memo:
        title = self._title(title)
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Audio file not found: {source}")
        memo_id = uuid.uuid4().hex
        final_path = self.audio_dir / f"{memo_id}.wav"
        handle, name = tempfile.mkstemp(prefix=".import-", suffix=".wav", dir=self.audio_dir)
        os.close(handle)
        staging = Path(name)
        published = False
        try:
            args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-protocol_whitelist", "file,pipe", "-i", str(source)]
            if start is not None:
                args.extend(["-ss", str(start), "-t", str(end - start)])
            args.extend(["-map", "0:a:0", "-vn", "-map_metadata", "-1", "-ac", "1", "-ar", "48000",
                         "-c:a", "pcm_s16le", str(staging)])
            _run_command(args)
            duration_text = _run_command([
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(staging),
            ], timeout=30, capture_stdout=True)
            try:
                duration = float(duration_text.strip())
            except ValueError as exc:
                raise RuntimeError("Could not determine the audio duration.") from exc
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("The file contains no usable audio.")
            peaks = _peaks(staging)
            created = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            os.replace(staging, final_path)
            published = True
            with closing(self._connect()) as db, db:
                db.execute("""INSERT INTO memos
                    (id,title,created_at,duration,audio_path,transcript,peaks)
                    VALUES (?,?,?,?,?,?,?)""",
                    (memo_id, title, created, duration, str(final_path.relative_to(self.root)), "", json.dumps(peaks)))
            return Memo(memo_id, title, created, duration, final_path, "", peaks)
        except BaseException:
            if published:
                final_path.unlink(missing_ok=True)
            raise
        finally:
            staging.unlink(missing_ok=True)

    def import_audio(self, path: Path, title: str | None = None) -> Memo:
        path = Path(path)
        return self._create(path, title if title is not None else path.stem)

    def rename(self, id: str, title: str) -> Memo:
        return self._update(id, "title", self._title(title))

    def _update(self, id: str, column: str, value: str) -> Memo:
        if column not in {"title", "transcript"}:
            raise ValueError("Invalid metadata field.")
        with closing(self._connect()) as db, db:
            cursor = db.execute(f"UPDATE memos SET {column}=? WHERE id=? AND deleted_at IS NULL", (value, id))
            if not cursor.rowcount:
                raise KeyError(f"Memo {id!r} was not found.")
        return self.get(id)

    def trim(self, id: str, start: float, end: float) -> Memo:
        memo = self.get(id)
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= memo.duration):
            raise ValueError("Choose a start and end inside the recording, with end after start.")
        return self._create(memo.audio_path, memo.title[:490] + " (trimmed)", start=start, end=end)

    def set_transcript(self, id: str, text: str) -> Memo:
        return self._update(id, "transcript", text)

    def export(self, id: str, destination: Path) -> None:
        memo = self.get(id)
        destination = Path(destination).expanduser().resolve()
        if destination == memo.audio_path.resolve():
            raise ValueError("Choose a destination other than the original recording.")
        if destination.is_relative_to(self.audio_dir) or destination.is_relative_to(self.trash_dir):
            raise ValueError("Export outside the library's managed audio and trash folders.")
        if destination.suffix.lower() not in {".wav", ".mp3"}:
            raise ValueError("Export supports .wav and .mp3 files.")
        if destination.exists():
            raise FileExistsError(f"A file already exists at {destination}. Choose a new name.")
        if not destination.parent.is_dir():
            raise FileNotFoundError(f"Destination folder not found: {destination.parent}")
        handle, name = tempfile.mkstemp(prefix=".omamemo-export-", suffix=destination.suffix.lower(), dir=destination.parent)
        os.close(handle)
        staging = Path(name)
        try:
            if destination.suffix.lower() == ".wav":
                shutil.copyfile(memo.audio_path, staging)
            else:
                _run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                              "-i", str(memo.audio_path), "-map_metadata", "-1", "-c:a", "libmp3lame", "-q:a", "2", str(staging)])
            # Hard-link publication is atomic and refuses a concurrently created file.
            os.link(staging, destination)
        finally:
            staging.unlink(missing_ok=True)

    def delete(self, id: str) -> None:
        moved = False
        source: Path | None = None
        target: Path | None = None
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM memos WHERE id=? AND deleted_at IS NULL", (id,)).fetchone()
                if row is None:
                    raise KeyError(f"Memo {id!r} was not found.")
                source = self.root / row["audio_path"]
                target = self.trash_dir / f"{id}.wav"
                os.replace(source, target)
                moved = True
                db.execute("UPDATE memos SET audio_path=?,deleted_at=? WHERE id=?",
                           (str(target.relative_to(self.root)), datetime.now(timezone.utc).isoformat(), id))
                db.commit()
            except BaseException:
                db.rollback()
                if moved:
                    os.replace(target, source)
                raise

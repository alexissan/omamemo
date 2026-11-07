"""Optional offline transcription using an explicitly selected whisper.cpp model."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from .core import Memo, _run_command


def is_available(executable: str = "whisper-cli") -> bool:
    return shutil.which(executable) is not None


def transcribe(memo: Memo, model_path: Path, executable: str = "whisper-cli") -> str:
    """Return plain text, without changing the memo or fetching any assets.

    whisper.cpp accepts 16 kHz PCM WAV. A separate scratch conversion leaves
    the high-quality library original untouched. The CLI writes text to a file
    so progress messages and diagnostic output cannot become the transcript.
    """
    model_path = Path(model_path).expanduser().resolve()
    if not model_path.is_file():
        raise FileNotFoundError(f"Select an existing local whisper.cpp model (.bin): {model_path}")
    if not memo.audio_path.is_file():
        raise FileNotFoundError(f"The recording's audio file was not found: {memo.audio_path}")
    cli = shutil.which(executable)
    if cli is None:
        raise RuntimeError("whisper-cli was not found. Install whisper.cpp to enable local transcription.")
    with tempfile.TemporaryDirectory(prefix="omamemo-transcribe-") as directory:
        scratch = Path(directory)
        audio = scratch / "input.wav"
        output = scratch / "transcript"
        _run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                      "-i", str(memo.audio_path.resolve()), "-map", "0:a:0", "-ac", "1", "-ar", "16000",
                      "-c:a", "pcm_s16le", str(audio)])
        _run_command([cli, "-m", str(model_path), "-f", str(audio), "-l", "auto",
                      "-otxt", "-of", str(output), "-nt"],
                     timeout=max(300, min(7200, memo.duration * 20)))
        text_file = output.with_suffix(".txt")
        if not text_file.is_file():
            raise RuntimeError("whisper-cli finished without writing a transcript. Check the model and CLI version.")
        return text_file.read_text(encoding="utf-8").strip()

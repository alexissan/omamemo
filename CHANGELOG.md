# Changelog

## 0.1.1

Initial public preview of Omamemo, an independent local voice memo app for Omarchy
and Linux.

### Added

- Native PySide6 interface with memo library, audio waveform, and transcript panel.
- Explicit PipeWire microphone capture and local WAV/SQLite storage.
- Playback, seeking, and playback speed controls through Qt Multimedia.
- FFmpeg audio import, WAV/MP3 export, and trimming to a new memo.
- Memo renaming and title/transcript search.
- Optional local whisper.cpp transcription with a user-selected model.
- Live Omarchy 3 and 4 palette loading, plus a built-in fallback.
- User-level installer, desktop launcher, icon, and data-preserving uninstaller.
- Recovery audio retention after interrupted captures or save failures.
- Automated backend and offscreen UI regression coverage.

### Fixed

- Accept a quiet PipeWire exit code 1 after an intentional recording stop only
  when the captured WAV validates, avoiding a false save failure.
- Keep worker completion and errors on the GUI thread and prevent closing during
  active work.
- Keep recording state checks responsive while a subprocess is stopping.
- Clamp trimmed copies to the actual recording duration.
- Restore playback after a failed move to Trash.
- Derive readable surfaces and controls for light Omarchy themes.

### Preview limitations

- Real microphone capture and Whisper model inference require further validation
  on target hardware; subprocess and transcription tests use controlled doubles.
- Trash recovery is a manual audio import; previous transcript metadata is not
  restored to the new memo.
- Shared 9p folders are unsuitable for the SQLite WAL library; use a local disk.

# Validation — 7 November 2026

- 39 automated tests and 7 subtests passed with Python 3.13 and PySide6 6.12 on macOS.
- Version 0.1.1 covers PipeWire's normal SIGINT exit code 1 using a process double and real FFmpeg import. Corrupt, truncated, empty, unexpected-exit and forced-stop captures remain rejected; a failed database save preserves the original recovery WAV.
- Real FFmpeg import, WAV/MP3 export, trimming and waveform extraction tested.
- Qt worker responsiveness, main-thread callbacks, search, recording state, light/dark theme contrast and live theme switching tested.
- Offscreen interaction tests process Qt events between brief Python sleeps to let worker threads run; native nested test waits intermittently stalled on macOS. Linux playback still needs a live check.
- Playback decoding advanced correctly with output muted; physical audio output was not tested.
- Installer syntax, install/update/uninstall and data retention checked with isolated fixtures.
- Dark and light interfaces visually inspected. The preview uses a generated tone and explicitly labelled demonstration transcript.

## Still requires the Omarchy VM

Real microphone recording, sound output, and whisper.cpp inference with a local model. Automated recorder and transcription tests substitute subprocesses; they do not establish hardware or model compatibility. This chat could inspect the VM configuration but could not control its window or connect to a guest shell.

The app does not include or download a transcription model. Recordings, playback, import, export and trimming work independently of transcription.

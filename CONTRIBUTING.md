# Contributing

Use Python 3.11 or newer and install FFmpeg so both `ffmpeg` and `ffprobe` are on
`PATH`. See the [README](README.md) for Omarchy system packages; the
[Linux CI workflow](.github/workflows/tests.yml) lists Ubuntu Qt runtime packages.
From the source directory, create an isolated development environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --editable . pytest
QT_QPA_PLATFORM=offscreen python -m pytest tests -q -ra
```

The suite uses real FFmpeg conversions for import, trim, export, and recording
save regressions. FFmpeg-dependent backend tests skip when media tools are
missing, so check the test summary before reporting a passing run. CI verifies
both tools and runs the full suite on Linux with Python 3.11 and 3.13.

Microphone processes and Whisper inference are simulated. Offscreen Qt tests
exercise interactions, worker callbacks, responsiveness, and theme behavior;
they do not verify real audio devices, playback output, Wayland integration, or
transcription accuracy. No test should activate a microphone or download models.

For changes affecting recording, playback, or desktop integration, also check the
app in an Omarchy session with a separate test library:

```sh
omamemo_check_dir="$(mktemp -d)"
python -m omamemo --data-dir "$omamemo_check_dir"
```

Explicitly record and stop a short memo, play and seek, import/export audio, and
confirm a trim creates a copy. Check microphone-unavailable errors for recording
changes. For transcription changes, test with `whisper-cli` and a local model you
already have. Keep this scratch library outside shared folders that lack SQLite
WAL support.

Keep pull requests focused. Explain the behavior changed, how you tested it, and
any manual checks you could not complete. Include a regression test for a bug
when it can reproduce the failure without devices or network access. Preserve
original recordings and retained recovery files when handling failures.

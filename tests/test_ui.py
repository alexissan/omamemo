"""Offscreen interaction tests using real Qt event dispatch and the real library."""
import math
import os
from pathlib import Path
import struct
import time
import wave

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QEvent, QThread, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog

from omamemo.app import MainWindow
from omamemo.core import Library
from omamemo.theme import load_palette, _brightness, _contrast


@pytest.fixture(scope='session')
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def wait_for(predicate, timeout=6000):
    deadline = time.monotonic() + timeout / 1000
    # Native QTest.qWait can stall immediate Python QThread callbacks on macOS.
    # Pump events without a nested native wait, giving workers normal GIL access.
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(.01)
    assert predicate(), 'Qt operation did not finish in time'


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / 'an idea.wav'
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b''.join(struct.pack('<h', int(9000 * math.sin(i * 0.16))) for i in range(32000)))
    return path


class FakeRecorder:
    is_recording = False
    elapsed = 2.0
    starts = 0

    def __init__(self, library, source):
        self.library, self.source = library, source

    def start(self):
        self.starts += 1
        self.is_recording = True

    def stop(self):
        self.is_recording = False
        return self.library.import_audio(self.source, 'Captured idea')

    def cancel(self):
        self.is_recording = False

    def poll(self):
        return None


@pytest.fixture
def window(qapp, tmp_path, audio_file):
    library = Library(tmp_path / 'library')
    recorder = FakeRecorder(library, audio_file)
    widget = MainWindow(library=library, recorder=recorder)
    widget.test_errors = []
    widget._show_error = lambda message: widget.test_errors.append(message)
    widget.show()
    yield widget
    wait_for(lambda: not widget.busy)
    recorder.is_recording = False
    widget.recording = False
    widget.close()
    widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()
    time.sleep(.03)
    assert not widget.test_errors, widget.test_errors


def test_empty_library_never_starts_microphone(window):
    assert window.memo_list.count() == 0
    assert window.pages.currentIndex() == 0
    assert window.recorder.starts == 0
    assert window.record_button.isEnabled()


def test_import_is_responsive_and_callback_on_gui_thread(window, audio_file, monkeypatch, qapp):
    original = window.library.import_audio
    def slow_import(path):
        time.sleep(.15)
        return original(path)
    monkeypatch.setattr(window.library, 'import_audio', slow_import)
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a, **k: (str(audio_file), ''))
    callback_threads = []
    saved = window._saved
    def on_saved(memo):
        callback_threads.append(QThread.currentThread())
        saved(memo)
    window._saved = on_saved
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    window.import_button.click()
    assert window.busy
    assert not window.record_button.isEnabled()
    assert not window.close()  # A worker cannot be destroyed by closing the window.
    wait_for(lambda: not window.busy)
    timer.stop()
    assert len(ticks) >= 5
    assert callback_threads == [qapp.thread()]
    assert window.memo_list.count() == 1
    assert window.current.title == 'an idea'
    assert window.pages.currentIndex() == 1


def test_transcript_search_and_trim_preserve_original(window, audio_file):
    memo = window.library.import_audio(audio_file, 'Morning idea')
    window.library.set_transcript(memo.id, 'Remember the lantern on the desk.')
    window.refresh_library(select_id=memo.id)
    window.search.setText('LANTERN')
    assert window.memo_list.count() == 1
    assert 'lantern' in window.transcript.toPlainText()
    window.search.setText('missing sentence')
    assert window.memo_list.count() == 0
    assert window.current is None
    window.search.clear()
    window.trim_button.click()
    assert not window.trim_panel.isHidden()
    window.trim_start.setValue(.25)
    window.trim_end.setValue(1.0)
    window.trim_save.click()
    wait_for(lambda: not window.busy)
    assert len(window.library.list_memos()) == 2
    assert window.current.id != memo.id
    assert window.current.duration == pytest.approx(.75, abs=.03)
    assert window.library.get(memo.id).duration == pytest.approx(2)


def test_explicit_recording_and_edit_shortcut_guard(window):
    window.search.setFocus()
    window._shortcut('Ctrl+R', window.toggle_recording)
    assert window.recorder.starts == 0
    window.record_button.click()
    wait_for(lambda: not window.busy)
    assert window.recording
    assert window.pages.currentIndex() == 2
    assert 'Stop' in window.record_button.text()
    window.record_button.click()
    wait_for(lambda: not window.busy)
    assert not window.recording
    assert window.current.title == 'Captured idea'
    assert window.memo_list.count() == 1


def test_error_delivered_on_gui_thread(window, qapp):
    errors = []
    window._show_error = lambda message: errors.append((message, QThread.currentThread()))
    def fail():
        raise RuntimeError('Decoder could not read this file.')
    window._run('Importing…', fail)
    wait_for(lambda: not window.busy)
    assert errors == [('Decoder could not read this file.', qapp.thread())]
    assert window.record_button.isEnabled()


def test_light_omarchy_palette_semantics(tmp_path):
    path = tmp_path / '.local/state/omarchy/current/theme/colors.toml'
    path.parent.mkdir(parents=True)
    path.write_text('background = "#faf4ed"\nforeground = "#575279"\ndark_background = "#f2e9e1"\nlighter_background = "#fffaf3"\nmuted = "#797593"\nblue = "#286983"\nred = "#b4637a"\n')
    palette = load_palette(tmp_path)
    assert palette.surface == '#f2e9e1'
    assert palette.elevated == '#fffaf3'
    assert palette.accent == '#286983'
    assert _contrast(palette.muted, palette.surface) >= 4.5
    assert _brightness(palette.surface) > .8
    # Missing semantic surfaces are derived from the light background, never dark fallback.
    path.write_text('background = "#ffffff"\nforeground = "#202020"\n')
    palette = load_palette(tmp_path)
    assert _brightness(palette.surface) > .8
    assert _brightness(palette.elevated) > .8
    assert _brightness(palette.muted) < .5


def test_omarchy_border_muted_is_not_used_for_small_text(tmp_path):
    path = tmp_path / '.local/state/omarchy/current/theme/colors.toml'
    path.parent.mkdir(parents=True)
    path.write_text('background = "#1a1b26"\nforeground = "#c0caf5"\nmuted = "#414868"\n')
    palette = load_palette(tmp_path)
    assert _contrast(palette.muted, palette.background) >= 4.5
    assert _contrast(palette.muted, palette.surface) >= 4.5
    path.write_text('background = "#1a1b26"\nforeground = "#c0caf5"\nmuted = "#414868"\ndark_foreground = "#a9b1d6"\n')
    assert load_palette(tmp_path).muted == '#a9b1d6'


def test_live_theme_reload_updates_inline_labels(window, monkeypatch):
    from PySide6.QtWidgets import QLabel
    from omamemo import app as app_module
    from omamemo.theme import Palette
    previous = window.palette
    affected = [label for label in window.findChildren(QLabel) if previous.muted in label.styleSheet()]
    assert affected
    light = Palette(background='#faf4ed', surface='#fffaf3', elevated='#f2e9e1', foreground='#575279', muted='#676285', accent='#286983', red='#b4637a', green='#56949f', border='#d5d0d4', source='test-light')
    monkeypatch.setattr(app_module, 'load_palette', lambda: light)
    window._reload_theme()
    assert window.palette == light
    assert window.waveform.palette == light
    for label in affected:
        assert light.muted in label.styleSheet()
        assert previous.muted not in label.styleSheet()

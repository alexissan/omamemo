"""Native Qt desktop audio notebook. Recording begins only on explicit user action."""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
from typing import Callable

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QTimer, QUrl, Signal, Slot, QSize, QFileSystemWatcher
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QStackedWidget,
    QToolButton, QVBoxLayout, QWidget,
)

from .core import Library, Memo
from .recorder import Recorder
from .transcribe import transcribe
from .theme import Palette, load_palette, stylesheet
from .widgets import MemoRow, Waveform, ElidingLabel, clock_text


class Worker(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, function: Callable):
        super().__init__()
        self.function = function

    @Slot()
    def run(self):
        try:
            self.result.emit(self.function())
        except Exception as exc:
            self.error.emit(str(exc) or type(exc).__name__)
        finally:
            self.finished.emit()


def _label(text: str, name: str | None = None) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    return widget


def _date(value: str, full=False) -> str:
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone()
        return dt.strftime('%d %b %Y · %H:%M' if full else '%d %b · %H:%M')
    except ValueError:
        return value


class MainWindow(QMainWindow):
    def __init__(self, data_dir: Path | None = None, *, library=None, recorder=None):
        super().__init__()
        self.library = library if library is not None else Library(data_dir)
        self.recorder = recorder if recorder is not None else Recorder(self.library)
        self.settings = QSettings('omamemo', 'omamemo')
        self.palette = load_palette()
        self.current: Memo | None = None
        self.jobs: list[tuple[QThread, Worker]] = []
        self.busy = False
        self.recording = False
        self._success_callback = None
        self.close_after_job = False
        self.setWindowTitle('Omamemo — local audio notebook')
        self.resize(1080, 750)
        self.setMinimumSize(860, 700)
        self.setStyleSheet(stylesheet(self.palette))
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.positionChanged.connect(self._position_changed)
        self.player.playbackStateChanged.connect(self._playback_changed)
        self.player.errorOccurred.connect(self._playback_error)
        self._build()
        self._shortcuts()
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._watch_theme()
        self.refresh_library()

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        shell = QHBoxLayout(central)
        shell.setSpacing(0)
        shell.setContentsMargins(0, 0, 0, 0)
        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(272)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(20, 28, 20, 20)
        side.setSpacing(18)
        brand = QHBoxLayout()
        icon = _label('▥')
        icon.setStyleSheet(f'color: {self.palette.accent}; font-size: 29px;')
        brand.addWidget(icon)
        brand.addWidget(_label('omamemo', 'brand'))
        brand.addStretch()
        side.addLayout(brand)
        side.addWidget(_label('YOUR LOCAL AUDIO NOTEBOOK', 'eyebrow'))
        side.addSpacing(7)
        self.search = QLineEdit()
        self.search.setObjectName('search')
        self.search.setPlaceholderText('Search memos…')
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName('Search titles and transcripts')
        self.search.textChanged.connect(self.refresh_library)
        side.addWidget(self.search)
        countline = QHBoxLayout()
        countline.addWidget(_label('LIBRARY', 'sectionTitle'))
        countline.addStretch()
        self.count_label = _label('0', 'muted')
        countline.addWidget(self.count_label)
        side.addLayout(countline)
        self.memo_list = QListWidget()
        self.memo_list.setSpacing(2)
        self.memo_list.setAccessibleName('Memo library')
        self.memo_list.currentItemChanged.connect(self._selection_changed)
        side.addWidget(self.memo_list, 1)
        self.empty_search = _label('No matching memos.\nTry a title or transcript word.', 'muted')
        self.empty_search.setWordWrap(True)
        self.empty_search.hide()
        side.addWidget(self.empty_search)
        self.import_button = QPushButton('＋  Import audio')
        self.import_button.clicked.connect(self.import_audio)
        side.addWidget(self.import_button)
        privacy = _label('●  STORED ON THIS DEVICE', 'muted')
        privacy.setStyleSheet(f'color: {self.palette.muted}; font-size: 9px; letter-spacing: 1px;')
        side.addWidget(privacy)
        shell.addWidget(sidebar)
        main = QWidget()
        layout = QVBoxLayout(main)
        layout.setContentsMargins(32, 27, 32, 16)
        layout.setSpacing(20)
        topline = QHBoxLayout()
        topline.addWidget(_label('A LITTLE SPACE TO THINK.', 'eyebrow'))
        topline.addStretch()
        self.record_button = QPushButton('●  Record')
        self.record_button.setObjectName('record')
        self.record_button.setToolTip('Start recording from the default microphone (Ctrl+R)')
        self.record_button.clicked.connect(self.toggle_recording)
        topline.addWidget(self.record_button)
        layout.addLayout(topline)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_icon = _label('│ ▂ ▆ ▃ █ ▅ ▂ │')
        empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_icon.setStyleSheet(f'color: {self.palette.accent}; font-size: 25px; padding-bottom: 18px;')
        el.addWidget(empty_icon)
        empty_title = _label('Make room for a thought.')
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title.setStyleSheet('font-size: 23px; font-weight: 600;')
        el.addWidget(empty_title)
        empty_text = _label('Record an idea, a conversation, a moment.\nYour audio and transcripts stay on your device.', 'muted')
        empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_text.setStyleSheet(f'color: {self.palette.muted}; padding: 15px 0;')
        el.addWidget(empty_text)
        empty_hint = _label('● Record above  ·  or import an audio file', 'muted')
        empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(empty_hint)
        self.pages.addWidget(empty)
        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(19)
        title_row = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(9)
        self.title_label = ElidingLabel()
        self.title_label.setObjectName('memoTitle')
        titles.addWidget(self.title_label)
        self.date_label = _label('', 'muted')
        titles.addWidget(self.date_label)
        title_row.addLayout(titles, 1)
        self.more_button = QToolButton()
        self.more_button.setText('•••')
        self.more_button.setToolTip('Memo actions')
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = QMenu(self)
        self.rename_action = self.menu.addAction('Rename…', self.rename_memo)
        self.export_action = self.menu.addAction('Export audio…', self.export_memo)
        self.menu.addSeparator()
        self.delete_action = self.menu.addAction('Move to Trash…', self.delete_memo)
        self.more_button.setMenu(self.menu)
        title_row.addWidget(self.more_button, 0, Qt.AlignmentFlag.AlignTop)
        dl.addLayout(title_row)
        card = QFrame()
        card.setObjectName('waveCard')
        card.setMinimumHeight(253)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 17, 20, 16)
        cl.setSpacing(10)
        wave_header = QHBoxLayout()
        self.audio_label = _label('AUDIO MEMO', 'eyebrow')
        wave_header.addWidget(self.audio_label)
        wave_header.addStretch()
        self.format_label = _label('LOCAL FILE', 'muted')
        self.format_label.setStyleSheet(f'color: {self.palette.muted}; font-size: 10px;')
        wave_header.addWidget(self.format_label)
        cl.addLayout(wave_header)
        self.waveform = Waveform(self.palette)
        self.waveform.seek.connect(self._seek)
        cl.addWidget(self.waveform)
        controls = QHBoxLayout()
        self.play_button = QPushButton('▶')
        self.play_button.setObjectName('play')
        self.play_button.setFixedSize(44, 44)
        self.play_button.setAccessibleName('Play or pause memo')
        self.play_button.clicked.connect(self.toggle_playback)
        controls.addWidget(self.play_button)
        controls.addSpacing(7)
        self.elapsed_label = _label('00:00')
        self.duration_label = _label('/ 00:00', 'muted')
        controls.addWidget(self.elapsed_label)
        controls.addWidget(self.duration_label)
        controls.addStretch()
        self.speed = QComboBox()
        self.speed.setAccessibleName('Playback speed')
        for rate in (0.75, 1.0, 1.25, 1.5, 2.0):
            self.speed.addItem(f'{rate:g}×', rate)
        self.speed.setCurrentIndex(1)
        self.speed.currentIndexChanged.connect(lambda: self.player.setPlaybackRate(self.speed.currentData()))
        controls.addWidget(self.speed)
        self.trim_button = QToolButton()
        self.trim_button.setText('Trim')
        self.trim_button.setCheckable(True)
        self.trim_button.toggled.connect(self._toggle_trim)
        controls.addWidget(self.trim_button)
        cl.addLayout(controls)
        self.trim_panel = QFrame()
        self.trim_panel.setObjectName('trimPanel')
        tl = QHBoxLayout(self.trim_panel)
        tl.setContentsMargins(0, 15, 0, 0)
        tl.addWidget(_label('From', 'muted'))
        self.trim_start = QDoubleSpinBox()
        self.trim_start.setDecimals(2)
        self.trim_start.setSuffix(' s')
        self.trim_start.setAccessibleName('Trim start in seconds')
        tl.addWidget(self.trim_start)
        tl.addWidget(_label('to', 'muted'))
        self.trim_end = QDoubleSpinBox()
        self.trim_end.setDecimals(2)
        self.trim_end.setSuffix(' s')
        self.trim_end.setAccessibleName('Trim end in seconds')
        tl.addWidget(self.trim_end)
        tl.addStretch()
        self.trim_save = QPushButton('Save copy')
        self.trim_save.setObjectName('primary')
        self.trim_save.clicked.connect(self.trim_memo)
        tl.addWidget(self.trim_save)
        cl.addWidget(self.trim_panel)
        self.trim_panel.hide()
        dl.addWidget(card)
        transcript_header = QHBoxLayout()
        transcript_header.addWidget(_label('TRANSCRIPT', 'sectionTitle'))
        transcript_header.addStretch()
        self.model_button = QToolButton()
        self.model_button.setObjectName('flat')
        self.model_button.setText('Model…')
        self.model_button.setToolTip('Choose a local whisper.cpp model')
        self.model_button.clicked.connect(self.choose_model)
        transcript_header.addWidget(self.model_button)
        self.transcribe_button = QPushButton('Transcribe locally')
        self.transcribe_button.clicked.connect(self.transcribe_memo)
        transcript_header.addWidget(self.transcribe_button)
        dl.addLayout(transcript_header)
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setAccessibleName('Memo transcript')
        self.transcript.setPlaceholderText('Turn this recording into something you can search.\n\nChoose a local Whisper model, then transcribe on your device.')
        dl.addWidget(self.transcript, 1)
        self.transcript_note = _label('LOCAL WHISPER  ·  NO UPLOADS', 'muted')
        self.transcript_note.setStyleSheet(f'color: {self.palette.muted}; font-size: 9px; letter-spacing: 1px;')
        dl.addWidget(self.transcript_note)
        self.pages.addWidget(detail)
        # Dedicated recording state keeps a selected memo safe from temporary capture UI.
        capture = QWidget()
        cap = QVBoxLayout(capture)
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rec_label = _label('●  RECORDING')
        rec_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rec_label.setStyleSheet(f'color: {self.palette.red}; letter-spacing: 3px; font-size: 12px;')
        cap.addWidget(rec_label)
        self.record_time = _label('00:00')
        self.record_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.record_time.setStyleSheet('font-size: 56px; padding: 30px 0;')
        cap.addWidget(self.record_time)
        self.capture_wave = Waveform(self.palette)
        self.capture_wave.recording = True
        self.capture_wave.setCursor(Qt.CursorShape.ArrowCursor)
        self.capture_wave.setToolTip('Recording activity indicator; this is not a microphone level meter')
        cap.addWidget(self.capture_wave)
        cap.addWidget(_label('Recording activity · default microphone', 'muted'), 0, Qt.AlignmentFlag.AlignCenter)
        cap.addWidget(_label('Stop when you’re ready. Your memo will be saved locally.', 'muted'), 0, Qt.AlignmentFlag.AlignCenter)
        self.pages.addWidget(capture)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        footer = QHBoxLayout()
        self.status = _label('Ready when you are.', 'status')
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.shortcut_label = _label('Space play  ·  Ctrl R record', 'shortcut')
        self.shortcut_label.setStyleSheet(f'color: {self.palette.muted}; font-size: 10px;')
        footer.addWidget(self.shortcut_label)
        layout.addLayout(footer)
        shell.addWidget(main, 1)

    def _shortcuts(self):
        self.shortcuts = []
        for key, action in [('Space', self.toggle_playback), ('Ctrl+R', self.toggle_recording), ('Ctrl+F', self.focus_search)]:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(lambda action=action, key=key: self._shortcut(key, action))
            self.shortcuts.append(shortcut)
        QApplication.instance().focusChanged.connect(self._update_shortcut_context)

    def _update_shortcut_context(self, previous=None, current=None):
        focus = current or QApplication.focusWidget()
        editing = isinstance(focus, (QLineEdit, QPlainTextEdit, QDoubleSpinBox))
        for shortcut in self.shortcuts[:2]:
            shortcut.setEnabled(not editing)

    def _shortcut(self, key, action):
        focus = QApplication.focusWidget()
        if key != 'Ctrl+F' and isinstance(focus, (QLineEdit, QPlainTextEdit, QDoubleSpinBox)):
            return
        action()

    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def _watch_theme(self):
        self.theme_watcher = QFileSystemWatcher(self)
        self.theme_paths = [Path.home() / path for path in ('.local/state/omarchy/current/theme/colors.toml', '.config/omarchy/current/theme/colors.toml')]
        # Watch parents as well: a theme switch replaces a symlink or the whole file.
        for path in self.theme_paths:
            for candidate in (path, path.parent, path.parent.parent):
                if candidate.exists() and str(candidate) not in self.theme_watcher.files() + self.theme_watcher.directories():
                    self.theme_watcher.addPath(str(candidate))
        self.theme_watcher.fileChanged.connect(self._reload_theme)
        self.theme_watcher.directoryChanged.connect(self._reload_theme)
        self.theme_poll = QTimer(self)
        self.theme_poll.timeout.connect(self._reload_theme)
        self.theme_poll.start(3000)

    def _reload_theme(self):
        palette = load_palette()
        if palette != self.palette:
            previous = self.palette
            # A few compact labels have per-widget font sizing and palette color.
            for widget in self.findChildren(QWidget):
                css = widget.styleSheet()
                if css:
                    for index, field in enumerate(('background', 'surface', 'elevated', 'foreground', 'muted', 'accent', 'red', 'green', 'border')):
                        css = css.replace(getattr(previous, field), f'__palette_{index}__')
                    for index, field in enumerate(('background', 'surface', 'elevated', 'foreground', 'muted', 'accent', 'red', 'green', 'border')):
                        css = css.replace(f'__palette_{index}__', getattr(palette, field))
                    widget.setStyleSheet(css)
            self.palette = palette
            self.setStyleSheet(stylesheet(palette))
            for wave in (self.waveform, self.capture_wave):
                wave.palette = palette
                wave.update()
            self.refresh_library()

    @Slot()
    def refresh_library(self, _query=None, select_id: str | None = None):
        old_id = select_id or (self.current.id if self.current else None)
        try:
            memos = self.library.list_memos(self.search.text())
        except Exception as exc:
            self._show_error(str(exc))
            return
        self.memo_list.blockSignals(True)
        self.memo_list.clear()
        selected = None
        for memo in memos:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, memo.id)
            item.setToolTip(memo.title)
            item.setSizeHint(QSize(225, 76))
            self.memo_list.addItem(item)
            self.memo_list.setItemWidget(item, MemoRow(memo.title, _date(memo.created_at), memo.duration, self.palette))
            if memo.id == old_id:
                selected = item
        self.count_label.setText(str(len(memos)))
        self.empty_search.setVisible(not memos and bool(self.search.text()))
        if selected is None and memos:
            selected = self.memo_list.item(0)
        self.memo_list.setCurrentItem(selected)
        self.memo_list.blockSignals(False)
        if not self.recording:
            if selected:
                self._load_memo(self.library.get(selected.data(Qt.ItemDataRole.UserRole)))
            else:
                self.current = None
                self.player.stop()
                self.pages.setCurrentIndex(0)
        self._update_controls()

    def _selection_changed(self, item, previous):
        if item and not self.recording and not self.busy:
            try:
                self._load_memo(self.library.get(item.data(Qt.ItemDataRole.UserRole)))
            except Exception as exc:
                self._show_error(str(exc))

    def _load_memo(self, memo: Memo):
        changed = not self.current or self.current.id != memo.id
        self.current = memo
        if changed:
            self.player.stop()
            self.player.setSource(QUrl.fromLocalFile(str(memo.audio_path)))
            self.elapsed_label.setText('00:00')
        self.title_label.setText(memo.title)
        self.date_label.setText(_date(memo.created_at, True))
        self.format_label.setText(f'{memo.audio_path.suffix.lstrip(".").upper()}  ·  LOCAL FILE')
        self.duration_label.setText('/ ' + clock_text(memo.duration))
        if changed:
            self.waveform.set_audio(memo.peaks)
            self.trim_button.setChecked(False)
            self.trim_start.setRange(0, memo.duration)
            self.trim_end.setRange(0, memo.duration)
            self.trim_start.setValue(0)
            self.trim_end.setValue(memo.duration)
        self.transcript.setPlainText(memo.transcript)
        self.transcribe_button.setText('Transcribe again' if memo.transcript else 'Transcribe locally')
        words = len(memo.transcript.split())
        self.transcript_note.setText(f'{words} WORDS  ·  LOCAL WHISPER  ·  NO UPLOADS' if words else 'LOCAL WHISPER  ·  NO UPLOADS')
        self.pages.setCurrentIndex(1)
        self._update_controls()

    def _update_controls(self):
        recording = self.recording
        idle = not self.busy and not recording
        self.import_button.setEnabled(idle)
        self.memo_list.setEnabled(idle)
        self.search.setEnabled(idle)
        for widget in (self.more_button, self.play_button, self.speed, self.trim_button, self.trim_save, self.transcribe_button, self.model_button):
            widget.setEnabled(idle and self.current is not None)
        self.record_button.setEnabled(not self.busy)
        self.record_button.setText('■  Stop & save' if recording else '●  Record')
        self.record_button.setToolTip('Stop recording and save the memo (Ctrl+R)' if recording else 'Start recording from the default microphone (Ctrl+R)')
        self.progress.setVisible(self.busy)

    def _run(self, description: str, function: Callable, success: Callable | None = None):
        if self.busy:
            return
        self.busy = True
        self.status.setText(description)
        self._update_controls()
        thread = QThread(self)
        worker = Worker(function)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        self._success_callback = success
        worker.result.connect(self._job_result)
        worker.error.connect(self._task_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        pair = (thread, worker)
        self.jobs.append(pair)
        thread.finished.connect(self._job_finished)
        thread.start()

    @Slot(object)
    def _job_result(self, result):
        if self._success_callback:
            self._success_callback(result)

    @Slot()
    def _job_finished(self):
        thread = self.sender()
        pair = next((pair for pair in self.jobs if pair[0] is thread), None)
        if pair is None:
            return
        if pair in self.jobs:
            self.jobs.remove(pair)
        pair[0].deleteLater()
        self.busy = bool(self.jobs)
        self.recording = self.recorder.is_recording
        self._success_callback = None
        self._update_controls()
        if self.close_after_job and not self.busy and not self.recording:
            QTimer.singleShot(0, self.close)

    @Slot(str)
    def _task_error(self, message):
        self.close_after_job = False
        self.recording = self.recorder.is_recording
        self._show_error(message)
        if not self.recording:
            self.pages.setCurrentIndex(1 if self.current else 0)

    def _show_error(self, message):
        self.status.setText('Error: ' + message)
        QMessageBox.warning(self, 'Omamemo', message)

    def _saved(self, memo):
        self.recording = False
        self.search.clear()
        self.refresh_library(select_id=memo.id)
        self.status.setText('Saved on this device.')

    def import_audio(self):
        if self.busy or self.recording:
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Import audio', '', 'Audio (*.wav *.flac *.mp3 *.m4a *.ogg *.opus *.aac *.aiff *.wma);;All files (*)')
        if path:
            self._run('Importing audio…', lambda: self.library.import_audio(Path(path)), self._saved)

    def toggle_recording(self):
        if self.busy:
            return
        if self.recording:
            self._run('Saving recording…', self.recorder.stop, self._saved)
        else:
            self.player.pause()
            self._run('Opening the default microphone…', self.recorder.start, self._record_started)

    def _record_started(self, _result):
        self.recording = True
        self.record_time.setText('00:00')
        self.pages.setCurrentIndex(2)
        self.status.setText('Recording from the default microphone. Press Ctrl R to stop.')

    def _tick(self):
        if not self.busy and self.recording:
            error = self.recorder.poll()
            if error:
                self._task_error(error)
                self._update_controls()
                return
            self.record_time.setText(clock_text(self.recorder.elapsed))
            self.capture_wave.phase += 0.25
            self.capture_wave.update()

    def toggle_playback(self):
        if not self.current or self.busy or self.recording:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self.player.duration() and self.player.position() >= self.player.duration():
                self.player.setPosition(0)
            self.player.play()

    def _seek(self, fraction: float):
        if self.current and not self.busy and not self.recording:
            self.player.setPosition(int(fraction * self.current.duration * 1000))

    def _position_changed(self, position):
        self.elapsed_label.setText(clock_text(position / 1000))
        if self.current:
            self.waveform.set_progress(position / max(1, self.current.duration * 1000))

    def _playback_changed(self, state):
        self.play_button.setText('Ⅱ' if state == QMediaPlayer.PlaybackState.PlayingState else '▶')

    def _playback_error(self, error, message):
        if error != QMediaPlayer.Error.NoError:
            self.status.setText('Playback unavailable: ' + message)

    def rename_memo(self):
        if self.current and not self.busy:
            memo = self.current
            title, ok = QInputDialog.getText(self, 'Rename memo', 'Title', text=memo.title)
            if ok and title.strip():
                try:
                    self.library.rename(memo.id, title.strip())
                    self.refresh_library(select_id=memo.id)
                    self.status.setText('Memo renamed.')
                except Exception as exc:
                    self._show_error(str(exc))

    def export_memo(self):
        if self.current and not self.busy:
            memo = self.current
            name = ''.join(c if c.isalnum() or c in ' -_.' else '_' for c in memo.title).strip('.') or 'memo'
            path, _ = QFileDialog.getSaveFileName(self, 'Export audio', name + memo.audio_path.suffix, 'Audio files (*)')
            if path:
                self._run('Exporting audio…', lambda: self.library.export(memo.id, Path(path)), lambda _: self.status.setText('Audio exported.'))

    def delete_memo(self):
        if self.current and not self.busy:
            memo = self.current
            reply = QMessageBox.question(self, 'Move memo to Trash?', f'“{memo.title}” will be moved to the library’s Trash folder.', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if reply == QMessageBox.StandardButton.Yes:
                self.player.stop()
                self.player.setSource(QUrl())
                try:
                    self.library.delete(memo.id)
                    self.current = None
                    self.refresh_library()
                    self.status.setText('Moved to the library’s Trash folder.')
                except Exception as exc:
                    self.player.setSource(QUrl.fromLocalFile(str(memo.audio_path)))
                    self._show_error(str(exc))

    def _toggle_trim(self, visible):
        self.trim_panel.setVisible(visible)

    def trim_memo(self):
        if self.current and not self.busy:
            memo = self.current
            start, end = self.trim_start.value(), min(self.trim_end.value(), memo.duration)
            if end <= start:
                self._show_error('The trim end must be after the start.')
                return
            self.player.pause()
            self._run('Saving trimmed copy…', lambda: self.library.trim(memo.id, start, end), self._saved)

    def choose_model(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose a local whisper.cpp model', self.settings.value('whisper_model', ''), 'Whisper models (*.bin *.gguf);;All files (*)')
        if path:
            self.settings.setValue('whisper_model', path)
            self.model_button.setToolTip(str(path))
            self.status.setText('Whisper model selected: ' + Path(path).name)
        return path

    def transcribe_memo(self):
        if not self.current or self.busy or self.recording:
            return
        model = self.settings.value('whisper_model', '', type=str)
        if not model or not Path(model).is_file():
            model = self.choose_model()
        if not model:
            return
        memo = self.current
        self.player.pause()
        def work():
            text = transcribe(memo, Path(model))
            self.library.set_transcript(memo.id, text)
            return self.library.get(memo.id)
        self._run('Transcribing locally… This can take a few minutes.', work, self._transcribed)

    def _transcribed(self, memo):
        self.refresh_library(select_id=memo.id)
        self.status.setText('Transcript saved. Search it from the library.')

    def closeEvent(self, event: QCloseEvent):
        if self.busy:
            event.ignore()
            self.status.setText('Please wait for the current operation to finish before closing.')
            return
        if self.recording:
            dialog = QMessageBox(self)
            dialog.setWindowTitle('Recording in progress')
            dialog.setText('Save this recording before closing?')
            save = dialog.addButton('Stop, save & close', QMessageBox.ButtonRole.AcceptRole)
            discard = dialog.addButton('Discard & close', QMessageBox.ButtonRole.DestructiveRole)
            dialog.addButton('Keep recording', QMessageBox.ButtonRole.RejectRole)
            dialog.setDefaultButton(save)
            dialog.exec()
            event.ignore()
            if dialog.clickedButton() == save:
                self.close_after_job = True
                self._run('Saving recording…', self.recorder.stop, self._saved)
            elif dialog.clickedButton() == discard:
                self.close_after_job = True
                self._run('Discarding recording…', self.recorder.cancel)
            return
        self.player.stop()
        self.player.setSource(QUrl())
        event.accept()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Omamemo — a local audio notebook for Omarchy')
    parser.add_argument('--data-dir', type=Path, help='Use an alternative memo library directory')
    parser.add_argument('--screenshot', type=Path, help='Capture the real app window after startup, then exit')
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('Omamemo')
    app.setOrganizationName('omamemo')
    app.setDesktopFileName('omamemo')
    try:
        window = MainWindow(args.data_dir)
    except Exception as exc:
        QMessageBox.critical(None, 'Unable to open Omamemo', str(exc))
        return 1
    window.show()
    if args.screenshot:
        def capture():
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(args.screenshot)):
                print(f'Unable to save screenshot to {args.screenshot}', file=sys.stderr)
                app.exit(1)
            else:
                window.close()
        QTimer.singleShot(1200, capture)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())

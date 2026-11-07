"""Omarchy palette discovery and restrained native Qt styling."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Palette:
    background: str = '#1a1b26'
    surface: str = '#16161e'
    elevated: str = '#24283b'
    foreground: str = '#c0caf5'
    muted: str = '#9aa5ce'
    accent: str = '#7aa2f7'
    red: str = '#f7768e'
    green: str = '#9ece6a'
    border: str = '#30354c'
    source: str = 'Tokyo Night'


def _color(value: object, fallback: str) -> str:
    if isinstance(value, str):
        value = value.strip().removeprefix('0x')
        if not value.startswith('#'):
            value = '#' + value
        if len(value) == 7 and all(c in '0123456789abcdefABCDEF' for c in value[1:]):
            return value
    return fallback


def _blend(a: str, b: str, amount: float) -> str:
    return '#' + ''.join(f'{round(int(a[i:i + 2], 16) * (1 - amount) + int(b[i:i + 2], 16) * amount):02x}' for i in (1, 3, 5))


def _brightness(color: str) -> float:
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def _luminance(color: str) -> float:
    rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in rgb]
    return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def _contrast(a: str, b: str) -> float:
    light, dark = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (light + .05) / (dark + .05)


def _on(color: str) -> str:
    return max(('#16161e', '#ffffff'), key=lambda text: _contrast(text, color))


def _secondary(candidate: str, bg: str, surface: str, fg: str) -> str:
    if min(_contrast(candidate, bg), _contrast(candidate, surface)) >= 4.5:
        return candidate
    # Omarchy's `muted` is often a terminal border shade, unsuitable for small text.
    for amount in (.68, .75, .82, .9, 1.0):
        derived = _blend(bg, fg, amount)
        if min(_contrast(derived, bg), _contrast(derived, surface)) >= 4.5:
            return derived
    return fg


def load_palette(home: Path | None = None) -> Palette:
    home = home or Path.home()
    fallback = Palette()
    for relative in ('.local/state/omarchy/current/theme/colors.toml', '.config/omarchy/current/theme/colors.toml'):
        path = home / relative
        try:
            data = tomllib.loads(path.read_text())
            data = data.get('colors', data)
            primary = data.get('primary', data)
            normal = data.get('normal', data)
            bg = _color(primary.get('background'), fallback.background)
            light = _brightness(bg) > 0.55
            fg = _color(primary.get('foreground'), '#25252d' if light else fallback.foreground)
            surface = _color(data.get('dark_background'), _blend(bg, '#ffffff' if light else '#000000', 0.18))
            elevated = _color(data.get('lighter_background'), _blend(bg, fg, 0.09))
            return Palette(
                background=bg,
                surface=surface,
                elevated=elevated,
                foreground=fg,
                muted=_secondary(_color(data.get('dark_foreground', data.get('muted')), _blend(bg, fg, 0.68)), bg, surface, fg),
                accent=_color(data.get('accent', data.get('blue', data.get('color4', normal.get('blue')))), fallback.accent),
                red=_color(data.get('red', data.get('color1', normal.get('red'))), fallback.red),
                green=_color(data.get('green', data.get('color2', normal.get('green'))), fallback.green),
                border=_color(data.get('border'), _blend(bg, fg, 0.18)),
                source=path.parent.resolve().name,
            )
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return fallback


def stylesheet(p: Palette) -> str:
    from PySide6.QtGui import QFontDatabase
    families = set(QFontDatabase.families())
    font = next((name for name in ('JetBrainsMono Nerd Font', 'JetBrains Mono', 'CaskaydiaMono Nerd Font', 'DejaVu Sans Mono', 'Monaco', 'Menlo', 'Liberation Mono') if name in families), 'monospace')
    return f'''
    QWidget {{ background: {p.background}; color: {p.foreground}; font-family: "{font}"; font-size: 12px; }}
    QMainWindow {{ background: {p.background}; }}
    QFrame#sidebar {{ background: {p.surface}; border-right: 1px solid {p.border}; }}
    QFrame#sidebar QWidget {{ background: transparent; }}
    QLabel#brand {{ font-size: 23px; font-weight: 700; letter-spacing: -1px; }}
    QLabel#eyebrow {{ color: {p.muted}; font-size: 10px; letter-spacing: 2px; }}
    QLabel#memoTitle {{ font-size: 25px; font-weight: 600; }}
    QLabel#muted, QLabel#status, QLabel#shortcut {{ color: {p.muted}; }}
    QLabel#sectionTitle {{ font-weight: 600; font-size: 11px; letter-spacing: 1px; }}
    QLineEdit, QDoubleSpinBox, QComboBox {{ border: 1px solid {p.border}; border-radius: 7px; background: {p.surface}; padding: 9px; selection-background-color: {p.accent}; }}
    QLineEdit:focus, QDoubleSpinBox:focus {{ border-color: {p.accent}; }}
    QLineEdit#search {{ padding: 11px; }}
    QPushButton, QToolButton {{ border: 1px solid {p.border}; border-radius: 7px; background: transparent; padding: 9px 13px; font-weight: 500; }}
    QPushButton:hover, QToolButton:hover {{ background: {p.elevated}; border-color: {p.muted}; }}
    QPushButton:disabled, QToolButton:disabled {{ color: {p.muted}; border-color: {p.border}; }}
    QPushButton#record {{ background: {p.red}; color: {_on(p.red)}; border: 0; font-weight: 700; padding: 12px 18px; }}
    QPushButton#record:hover {{ background: {_blend(p.red, p.foreground, 0.08)}; }}
    QPushButton#play {{ border-radius: 22px; background: {p.accent}; color: {_on(p.accent)}; font-size: 18px; border: 0; }}
    QPushButton#primary {{ background: {p.accent}; color: {_on(p.accent)}; border: none; }}
    QToolButton#flat {{ border: 0; color: {p.muted}; }}
    QToolButton:checked {{ background: {p.elevated}; border-color: {p.accent}; }}
    QFrame#waveCard {{ border: 1px solid {p.border}; border-radius: 12px; background: {p.surface}; }}
    QFrame#waveCard QLabel {{ background: transparent; }}
    QFrame#trimPanel {{ border-top: 1px solid {p.border}; }}
    QPlainTextEdit {{ border: 1px solid {p.border}; border-radius: 10px; background: {p.surface}; padding: 17px; font-size: 13px; line-height: 1.7; selection-background-color: {p.accent}; }}
    QListWidget {{ border: 0; outline: 0; background: transparent; }}
    QListWidget::item {{ border: 0; border-radius: 8px; margin: 3px 0; padding: 0; }}
    QListWidget::item:selected {{ background: {p.elevated}; }}
    QListWidget::item:hover {{ background: {p.elevated}; }}
    QMenu {{ background: {p.elevated}; border: 1px solid {p.border}; padding: 6px; }}
    QMenu::item {{ padding: 9px 20px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {p.background}; }}
    QScrollBar:vertical {{ width: 7px; background: transparent; }}
    QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 3px; min-height: 25px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QProgressBar {{ border: 0; background: {p.border}; border-radius: 2px; max-height: 3px; }}
    QProgressBar::chunk {{ background: {p.accent}; }}
    QToolTip {{ background: {p.elevated}; border: 1px solid {p.border}; color: {p.foreground}; }}
    QMessageBox QLabel {{ min-width: 300px; }}
    '''

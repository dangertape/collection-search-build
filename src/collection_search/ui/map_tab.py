"""Map tab: every document in the ticked collections on one picture, similar ones together.

The map is built on a worker thread (see similarity_map.py) and drawn by
MapCanvas, which paints the points straight into an image with numpy so
35,000 documents pan and zoom smoothly.
"""

from __future__ import annotations

import concurrent.futures
import os
import threading
from typing import Callable

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QSplitter, QToolTip, QVBoxLayout, QWidget,
)

from ..engine import Engine, NotReady
from ..similarity_map import Cancelled, SimilarityMap, empty_map

ROLE = Qt.UserRole
LIST_LIMIT = 300  # documents listed in the side panel at once

# Categorical palette (light, dark), in fixed order. Neighbouring themes never share a slot,
# and every theme is also named on the map and in the list, so colour is never the only cue.
PALETTE = [
    ("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"), ("#1baf7a", "#199e70"), ("#eda100", "#c98500"),
    ("#e87ba4", "#d55181"), ("#008300", "#008300"), ("#6250d6", "#9085e9"), ("#e34948", "#e66767"),
]


def _is_dark(color: QColor) -> bool:
    return color.lightnessF() < 0.5


def _rgb(hex_color: str) -> np.ndarray:
    c = QColor(hex_color)
    return np.array([c.red(), c.green(), c.blue()], np.float32)


class MapCanvas(QWidget):
    """Draws a SimilarityMap. Drag to pan, wheel to zoom, click a dot or a theme name."""

    doc_clicked = Signal(int)
    doc_activated = Signal(int)
    theme_clicked = Signal(int)
    cleared = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(320, 260)
        self.setFocusPolicy(Qt.StrongFocus)
        self.map: SimilarityMap = empty_map()
        self.selected_theme: int | None = None
        self.selected_doc: int | None = None
        self.scale = 1.0
        self.centre = np.zeros(2)
        self._screen = np.zeros((0, 2), np.float32)
        self._label_rects: list[tuple[QRectF, int]] = []
        self._press = None
        self._dragged = False
        self._image_buffer = None
        self.placeholder = ""

    # ---- data ------------------------------------------------------------------
    def set_map(self, m: SimilarityMap) -> None:
        keep_view = len(self.map) and len(m) and self.map.key[:1] == m.key[:1]
        self.map = m
        self.selected_theme = self.selected_doc = None
        if not keep_view:
            self.fit()
        self.update()

    def fit(self) -> None:
        m = self.map
        if not len(m):
            self.scale, self.centre = 1.0, np.zeros(2)
            return
        discs = np.array([[t.x - t.radius, t.y - t.radius, t.x + t.radius, t.y + t.radius] for t in m.themes])
        lo, hi = discs[:, :2].min(axis=0), discs[:, 2:].max(axis=0)
        self.centre = (lo + hi) / 2
        span = np.maximum(hi - lo, 1e-6)
        self.scale = 0.9 * min(max(self.width(), 1) / span[0], max(self.height() - 30, 1) / span[1])
        self.update()

    def select_theme(self, t: int | None) -> None:
        self.selected_theme, self.selected_doc = t, None
        self.update()

    def select_doc(self, i: int | None) -> None:
        self.selected_doc = i
        self.selected_theme = int(self.map.theme[i]) if i is not None else None
        self.update()

    # ---- coordinates -------------------------------------------------------------
    def _to_screen(self, xy: np.ndarray) -> np.ndarray:
        half = np.array([self.width() / 2, self.height() / 2])
        return (xy - self.centre) * self.scale + half

    def _to_map(self, x: float, y: float) -> np.ndarray:
        half = np.array([self.width() / 2, self.height() / 2])
        return (np.array([x, y]) - half) / self.scale + self.centre

    def _dot_radius(self) -> int:
        n = max(len(self.map), 1)
        base = 4 if n < 300 else 3 if n < 3000 else 2 if n < 15000 else 1
        return int(np.clip(base * (self.scale / max(self._fit_scale(), 1e-6)) ** 0.5, base, 6))

    def _fit_scale(self) -> float:
        m = self.map
        if not len(m):
            return 1.0
        extent = max(max(abs(t.x) + t.radius, abs(t.y) + t.radius) for t in m.themes) * 2
        return 0.9 * min(self.width(), self.height()) / max(extent, 1e-6)

    def _colors(self, dark: bool) -> np.ndarray:
        slots = np.array([t.color for t in self.map.themes], np.int64)
        table = np.stack([_rgb(PALETTE[s][1 if dark else 0]) for s in range(len(PALETTE))])
        return table[slots[self.map.theme]]

    # ---- painting --------------------------------------------------------------------
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        bg = self.palette().base().color()
        ink = self.palette().text().color()
        painter.fillRect(self.rect(), bg)
        m = self.map
        if not len(m):
            painter.setPen(self.palette().placeholderText().color())
            painter.drawText(self.rect().adjusted(20, 20, -20, -20), Qt.AlignCenter | Qt.TextWordWrap,
                             self.placeholder)
            self._screen = np.zeros((0, 2), np.float32)
            self._label_rects = []
            return
        dark = _is_dark(bg)
        self._screen = self._to_screen(m.xy).astype(np.float32)
        image = self._render_dots(bg, dark)
        painter.drawImage(0, 0, image)
        self._draw_labels(painter, bg, ink)
        if self.selected_doc is not None:
            x, y = self._screen[self.selected_doc]
            r = self._dot_radius() + 5
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(bg, 4))
            painter.drawEllipse(QPointF(x, y), r, r)
            painter.setPen(QPen(ink, 2))
            painter.drawEllipse(QPointF(x, y), r, r)
        painter.end()

    def _render_dots(self, bg: QColor, dark: bool) -> QImage:
        dpr = self.devicePixelRatioF()
        w, h = max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr))
        back = np.array([bg.red(), bg.green(), bg.blue()], np.float32)
        buf = np.empty((h, w, 4), np.uint8)
        buf[..., :3] = back.astype(np.uint8)
        buf[..., 3] = 255
        colors = self._colors(dark)
        pts = self._screen * dpr
        order = np.arange(len(pts))
        if self.selected_theme is not None:
            chosen = self.map.theme == self.selected_theme
            colors = np.where(chosen[:, None], colors, colors * 0.22 + back * 0.78)
            order = np.concatenate([np.flatnonzero(~chosen), np.flatnonzero(chosen)])  # chosen on top
        colors = colors.astype(np.uint8)
        r = max(1, int(round(self._dot_radius() * dpr)))
        px = np.rint(pts[order, 0]).astype(np.int64)
        py = np.rint(pts[order, 1]).astype(np.int64)
        col = colors[order]
        for dy in range(-r + 1, r):
            for dx in range(-r + 1, r):
                if dx * dx + dy * dy > (r - 0.5) ** 2:
                    continue
                x, y = px + dx, py + dy
                ok = (x >= 0) & (x < w) & (y >= 0) & (y < h)
                buf[y[ok], x[ok], :3] = col[ok]
        self._image_buffer = buf  # QImage below doesn't copy; keep the pixels alive while painting
        image = QImage(buf.data, w, h, w * 4, QImage.Format_RGBA8888)
        image.setDevicePixelRatio(dpr)
        return image

    def _draw_labels(self, painter: QPainter, bg: QColor, ink: QColor) -> None:
        painter.setRenderHint(QPainter.Antialiasing)
        self._label_rects = []
        placed: list[QRectF] = []
        themes = sorted(self.map.themes, key=lambda t: (t.index != self.selected_theme, -t.size))
        for t in themes:
            font = QFont(self.font())
            font.setBold(True)
            font.setPointSizeF(font.pointSizeF() * (1.15 if t.size > len(self.map) / 8 else 1.0))
            metrics = QFontMetrics(font)
            text = f"{t.label}  ({t.size:,})"
            cx, cy = self._to_screen(np.array([t.x, t.y]))
            top = cy - t.radius * self.scale - metrics.height() - 2
            rect = QRectF(cx - metrics.horizontalAdvance(text) / 2 - 6, top, metrics.horizontalAdvance(text) + 12,
                          metrics.height() + 4)
            if rect.top() < 2:  # disc reaches the top edge: put the name over the disc instead
                rect.moveTop(max(2.0, cy - rect.height() / 2))
            if any(rect.intersects(p) for p in placed) and t.index != self.selected_theme:
                continue  # crowded; this theme is still named in the list and on hover
            placed.append(rect)
            self._label_rects.append((rect, t.index))
            fill = QColor(bg)
            fill.setAlpha(225)
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 6, 6)
            swatch = QColor(PALETTE[t.color][1 if _is_dark(bg) else 0])
            painter.setBrush(swatch)
            painter.drawRoundedRect(QRectF(rect.left() + 1, rect.bottom() - 3, rect.width() - 2, 3), 1.5, 1.5)
            dim = self.selected_theme is not None and t.index != self.selected_theme
            text_color = QColor(ink)
            if dim:
                text_color.setAlphaF(0.55)
            painter.setFont(font)
            painter.setPen(text_color)
            painter.drawText(rect.adjusted(0, 0, 0, -2), Qt.AlignCenter, text)

    # ---- hit testing -----------------------------------------------------------------
    def doc_at(self, x: float, y: float, reach: float = 8.0) -> int | None:
        if not len(self._screen):
            return None
        d2 = ((self._screen - np.array([x, y], np.float32)) ** 2).sum(axis=1)
        i = int(np.argmin(d2))
        return i if d2[i] <= reach * reach else None

    def theme_at(self, x: float, y: float) -> int | None:
        for rect, t in self._label_rects:
            if rect.contains(QPointF(x, y)):
                return t
        p = self._to_map(x, y)
        for t in self.map.themes:
            if np.hypot(p[0] - t.x, p[1] - t.y) <= t.radius:
                return t.index
        return None

    # ---- mouse -------------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press = (event.position().x(), event.position().y(), self.centre.copy())
            self._dragged = False

    def mouseMoveEvent(self, event) -> None:
        x, y = event.position().x(), event.position().y()
        if self._press and event.buttons() & Qt.LeftButton:
            px, py, centre = self._press
            if abs(x - px) + abs(y - py) > 4:
                self._dragged = True
                self.setCursor(Qt.ClosedHandCursor)
            if self._dragged:
                self.centre = centre - np.array([x - px, y - py]) / self.scale
                self.update()
            return
        i = self.doc_at(x, y)
        if i is not None:
            theme = self.map.themes[int(self.map.theme[i])]
            QToolTip.showText(event.globalPosition().toPoint(),
                              f"<b>{_esc(os.path.basename(self.map.paths[i]))}</b><br>{_esc(self.map.paths[i])}"
                              f"<br>Theme: {_esc(theme.label)}", self)
            self.setCursor(Qt.PointingHandCursor)
        else:
            QToolTip.hideText()
            self.setCursor(Qt.PointingHandCursor if self.theme_at(x, y) is not None else Qt.ArrowCursor)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or not self._press:
            return
        self._press = None
        self.setCursor(Qt.ArrowCursor)
        if self._dragged:
            return
        x, y = event.position().x(), event.position().y()
        i = self.doc_at(x, y)
        if i is not None:
            self.select_doc(i)
            self.doc_clicked.emit(i)
            return
        t = self.theme_at(x, y)
        if t is not None:
            self.select_theme(t)
            self.theme_clicked.emit(t)
        else:
            self.select_theme(None)
            self.cleared.emit()

    def mouseDoubleClickEvent(self, event) -> None:
        i = self.doc_at(event.position().x(), event.position().y())
        if i is not None:
            self.doc_activated.emit(i)

    def wheelEvent(self, event) -> None:
        steps = event.angleDelta().y() / 120
        if not steps or not len(self.map):
            return
        x, y = event.position().x(), event.position().y()
        before = self._to_map(x, y)
        fit = self._fit_scale()
        self.scale = float(np.clip(self.scale * 1.25 ** steps, fit * 0.5, fit * 200))
        after = self._to_map(x, y)
        self.centre = self.centre + (before - after)
        self.update()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Minus):
            factor = 1.25 if event.key() != Qt.Key_Minus else 0.8
            self.scale *= factor
            self.update()
        elif event.key() in (Qt.Key_0, Qt.Key_Home):
            self.fit()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        old = event.oldSize()
        if old.width() <= 0 or old.height() <= 0:
            self.fit()
        super().resizeEvent(event)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _Bridge(QObject):
    done = Signal(int, object)
    failed = Signal(int, str)


class MapTab(QWidget):
    def __init__(self, engine: Engine, show_similar: Callable[[str], None] | None = None, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.show_similar = show_similar
        self.map: SimilarityMap = empty_map()
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="map")
        self._generation = 0
        self._cancel = threading.Event()
        self._busy = False
        self._stale = True
        self.bridge = _Bridge()
        self.bridge.done.connect(self._built)
        self.bridge.failed.connect(self._failed)

        self.message = QLabel("Documents that are about the same thing sit together. Click a dot or a theme, "
                              "double-click a dot to open it. Drag to move, scroll to zoom.")
        self.message.setWordWrap(True)
        redraw = QPushButton("Redraw")
        redraw.clicked.connect(self.refresh)
        fit = QPushButton("Fit")
        fit.clicked.connect(lambda: self.canvas.fit())
        top = QHBoxLayout()
        top.addWidget(self.message, 1)
        top.addWidget(fit)
        top.addWidget(redraw)

        self.canvas = MapCanvas()
        self.canvas.placeholder = "The map appears here once the index is ready."
        self.canvas.doc_clicked.connect(self._doc_selected)
        self.canvas.doc_activated.connect(self._open_index)
        self.canvas.theme_clicked.connect(self._theme_selected)
        self.canvas.cleared.connect(self._cleared)

        self.theme_list = QListWidget()
        self.theme_list.itemClicked.connect(lambda item: self._pick_theme(item.data(ROLE)))
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.doc_list = QListWidget()
        self.doc_list.currentItemChanged.connect(self._doc_list_changed)
        self.doc_list.itemDoubleClicked.connect(lambda item: self._open_index(item.data(ROLE)))
        self.open_button, self.similar_button = QPushButton("Open"), QPushButton("Find similar")
        self.open_button.clicked.connect(lambda: self._open_index(self.canvas.selected_doc))
        self.similar_button.clicked.connect(self._find_similar)
        buttons = QHBoxLayout()
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.similar_button)
        buttons.addStretch()

        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.addWidget(QLabel("Themes"))
        side_layout.addWidget(self.theme_list, 2)
        side_layout.addWidget(self.detail)
        side_layout.addWidget(self.doc_list, 3)
        side_layout.addLayout(buttons)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.canvas)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(split, 1)
        self._cleared()

    # ---- building -----------------------------------------------------------------
    def mark_stale(self) -> None:
        """Ticked collections or the index changed: redraw now if showing, else when next shown."""
        self._stale = True
        if self.isVisible():
            self.refresh()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._stale:
            self.refresh()

    def refresh(self) -> None:
        self._stale = False
        self._cancel.set()  # stop a build that is now out of date
        self._cancel = cancel = threading.Event()
        self._generation += 1
        generation = self._generation
        if not len(self.map):
            self.canvas.placeholder = "Drawing the map…"
            self.canvas.update()

        def work():
            try:
                built = self.engine.similarity_map(cancelled=cancel.is_set)
            except Cancelled:
                return
            except NotReady as exc:
                self.bridge.failed.emit(generation, str(exc))
            except Exception as exc:  # shown in the tab; the app keeps running
                self.bridge.failed.emit(generation, f"Could not draw the map: {exc}")
            else:
                self.bridge.done.emit(generation, built)

        self._pool.submit(work)

    def _failed(self, generation: int, text: str) -> None:
        if generation != self._generation:
            return
        self._stale = True  # try again next time the tab is shown
        self.canvas.placeholder = text
        if not len(self.map):
            self.canvas.update()
        self.message.setText(text)

    def _built(self, generation: int, built: SimilarityMap) -> None:
        if generation != self._generation or built is self.map:
            return
        self.map = built
        self.canvas.placeholder = "No indexed documents in the ticked collections yet."
        self.canvas.set_map(built)
        self.theme_list.clear()
        dark = _is_dark(self.palette().base().color())
        for t in built.themes:
            pix = QPixmap(14, 14)
            pix.fill(QColor(PALETTE[t.color][1 if dark else 0]))
            item = QListWidgetItem(QIcon(pix), f"{t.label}  ({t.size:,})")
            item.setToolTip(f"{t.label}: {t.size:,} documents" + (f", mostly in {t.folder}" if t.folder else ""))
            item.setData(ROLE, t.index)
            self.theme_list.addItem(item)
        if len(built):
            self.message.setText(f"{len(built):,} documents in {len(built.themes)} themes. Click a dot or a theme, "
                                 "double-click a dot to open it. Drag to move, scroll to zoom.")
        else:
            self.message.setText("No indexed documents in the ticked collections yet.")
        self._cleared()

    def shutdown(self) -> None:
        self._cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ---- selection -------------------------------------------------------------------
    def _pick_theme(self, t: int) -> None:
        self.canvas.select_theme(t)
        self._theme_selected(t)

    def _theme_selected(self, t: int) -> None:
        theme = self.map.themes[t]
        self._select_row(self.theme_list, t)
        folder = f" Most are in {theme.folder}." if theme.folder else ""
        self.detail.setText(f"<b>{_esc(theme.label)}</b>: {theme.size:,} documents, most typical first.{folder}")
        self._fill_docs(self.map.members(t)[:LIST_LIMIT])
        self._update_buttons()

    def _doc_selected(self, i: int) -> None:
        m = self.map
        self._select_row(self.theme_list, int(m.theme[i]))
        theme = m.themes[int(m.theme[i])]
        self.detail.setText(f"<b>{_esc(os.path.basename(m.paths[i]))}</b><br>{_esc(m.paths[i])}<br>"
                            f"Theme: {_esc(theme.label)}. Below: the documents next to it on the map.")
        self._fill_docs(m.near(i, LIST_LIMIT), current=i)
        self._update_buttons()

    def _cleared(self) -> None:
        self.theme_list.clearSelection()
        self.doc_list.clear()
        self.detail.setText("Pick a theme or a dot to list its documents." if len(self.map) else "")
        self._update_buttons()

    def _fill_docs(self, indices, current: int | None = None) -> None:
        self.doc_list.blockSignals(True)
        self.doc_list.clear()
        for i in indices:
            i = int(i)
            item = QListWidgetItem(os.path.basename(self.map.paths[i]))
            item.setToolTip(self.map.paths[i])
            item.setData(ROLE, i)
            self.doc_list.addItem(item)
            if i == current:
                self.doc_list.setCurrentItem(item)
        self.doc_list.blockSignals(False)

    def _doc_list_changed(self, item, _previous=None) -> None:
        if item is None:
            return
        i = item.data(ROLE)
        theme = self.canvas.selected_theme
        self.canvas.selected_doc = i
        self.canvas.selected_theme = theme if theme is not None else int(self.map.theme[i])
        self.canvas.update()
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = self.canvas.selected_doc is not None
        self.open_button.setEnabled(has)
        self.similar_button.setEnabled(has and self.show_similar is not None)

    @staticmethod
    def _select_row(widget: QListWidget, value) -> None:
        widget.blockSignals(True)
        for row in range(widget.count()):
            if widget.item(row).data(ROLE) == value:
                widget.setCurrentRow(row)
                break
        widget.blockSignals(False)

    # ---- actions -------------------------------------------------------------------------
    def _open_index(self, i: int | None) -> None:
        if i is not None and 0 <= i < len(self.map):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.map.paths[i]))

    def _find_similar(self) -> None:
        i = self.canvas.selected_doc
        if i is not None and self.show_similar:
            self.show_similar(self.map.paths[i])

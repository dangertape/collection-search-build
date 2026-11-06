"""Map tab: every document in the ticked collections on one picture, similar ones together.

The map is built on a worker thread (see similarity_map.py) and drawn by
MapCanvas, which paints the points straight into an image with numpy so
35,000 documents pan and zoom smoothly.

Picking a document also lists which of its passages match passages in other
documents (passages.py) and draws lines to those documents. The Passages view
draws one dot per passage instead, with lines between the closest ones.
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
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QPushButton, QSplitter, QTabWidget, QTextBrowser,
    QToolTip, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
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
        # Lines between dots: (from, to, similarity). `highlight` is the one picked in the side panel.
        self.lines: list[tuple[int, int, float]] = []
        self.highlight: tuple[int, int] | None = None

    # ---- data ------------------------------------------------------------------
    def set_map(self, m: SimilarityMap) -> None:
        keep_view = (len(self.map) and len(m) and self.map.key[:1] == m.key[:1]
                     and self.map.passages == m.passages)
        self.map = m
        self.selected_theme = self.selected_doc = None
        self.lines, self.highlight = [], None
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
        self.lines, self.highlight = [], None
        self.update()

    def select_doc(self, i: int | None) -> None:
        self.selected_doc = i
        self.selected_theme = int(self.map.theme[i]) if i is not None else None
        self.lines, self.highlight = [], None
        self.update()

    def set_lines(self, lines: list[tuple[int, int, float]], highlight: tuple[int, int] | None = None) -> None:
        self.lines, self.highlight = lines, highlight
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
        self._draw_lines(painter, ink)
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

    def _draw_lines(self, painter: QPainter, ink: QColor) -> None:
        """Lines for the picked dot's connections, or the strongest passage links when nothing is picked."""
        painter.setRenderHint(QPainter.Antialiasing)
        lines = self.lines
        faint = False
        if not lines and self.selected_doc is None and self.selected_theme is None and len(self.map.strongest):
            lines = [(int(a), int(b), float(v)) for a, b, v in self.map.strongest]
            faint = True
        if not lines:
            return
        sims = np.array([v for _, _, v in lines])
        lo, hi = float(sims.min()), float(sims.max())
        for a, b, v in sorted(lines, key=lambda line: line[2]):
            strength = (v - lo) / (hi - lo) if hi > lo else 1.0
            color = QColor(ink)
            picked = self.highlight in ((a, b), (b, a))
            if faint:
                color.setAlphaF(0.08 + 0.22 * strength)
                width = 1.0
            else:
                color.setAlphaF(0.95 if picked else 0.25 + 0.45 * strength)
                width = 3.0 if picked else 1.0 + 1.5 * strength
            painter.setPen(QPen(color, width))
            painter.drawLine(QPointF(*self._screen[a]), QPointF(*self._screen[b]))

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
            where = f" · {_esc(self.map.locations[i])}" if self.map.passages else ""
            excerpt = f"<br><i>{_esc(_short(self.map.texts[i], 160))}</i>" if self.map.passages else ""
            QToolTip.showText(event.globalPosition().toPoint(),
                              f"<b>{_esc(os.path.basename(self.map.paths[i]))}</b>{where}<br>{_esc(self.map.paths[i])}"
                              f"{excerpt}<br>Theme: {_esc(theme.label)}", self)
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


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


class _Bridge(QObject):
    done = Signal(int, object)
    failed = Signal(int, str)
    links = Signal(int, object)  # passage links of the picked document


class MapTab(QWidget):
    def __init__(self, engine: Engine, show_similar: Callable[[str], None] | None = None, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.show_similar = show_similar
        self.map: SimilarityMap = empty_map()
        self._index: dict[str, int] = {}  # path -> dot, in document view
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="map")
        self._links_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="links")
        self._generation = 0
        self._links_generation = 0
        self._cancel = threading.Event()
        self._stale = True
        self.bridge = _Bridge()
        self.bridge.done.connect(self._built)
        self.bridge.failed.connect(self._failed)
        self.bridge.links.connect(self._links_ready)

        self.view = QComboBox()
        self.view.addItem("Documents", False)
        self.view.addItem("Passages (detailed)", True)
        self.view.setToolTip("Passages: one dot per passage, with lines to the closest passages in other "
                             "documents. Takes longer to draw; past 20,000 passages an even sample is shown.")
        self.view.currentIndexChanged.connect(lambda _: self.refresh())
        self.message = QLabel(DOCS_HINT)
        self.message.setWordWrap(True)
        redraw = QPushButton("Redraw")
        redraw.clicked.connect(self.refresh)
        fit = QPushButton("Fit")
        fit.clicked.connect(lambda: self.canvas.fit())
        top = QHBoxLayout()
        top.addWidget(self.view)
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
        self.links_tree = QTreeWidget()
        self.links_tree.setHeaderLabels(["Passage", "Match"])
        self.links_tree.setWordWrap(True)
        self.links_tree.header().setStretchLastSection(False)
        self.links_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.links_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.links_tree.currentItemChanged.connect(self._link_picked)
        self.links_tree.itemDoubleClicked.connect(self._open_link)
        self.compare = QTextBrowser()
        self.compare.setOpenLinks(False)
        self.compare.hide()
        self.lists = QTabWidget()
        self.lists.addTab(self.doc_list, "Documents")
        self.lists.addTab(self.links_tree, "Connections")
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
        side_layout.addWidget(self.lists, 4)
        side_layout.addWidget(self.compare, 2)
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

    @property
    def passage_view(self) -> bool:
        return bool(self.view.currentData())

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
        passages = self.passage_view
        if not len(self.map) or self.map.passages != passages:
            self.message.setText("Finding the closest passages (can take a few seconds)…" if passages
                                 else "Drawing the map…")
            if not len(self.map):
                self.canvas.placeholder = "Drawing the map…"
                self.canvas.update()

        def work():
            try:
                built = self.engine.similarity_map(cancelled=cancel.is_set, passages=passages)
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
        self._index = {} if built.passages else {p: i for i, p in enumerate(built.paths)}
        what = "passages" if built.passages else "documents"
        self.canvas.placeholder = f"No indexed {what} in the ticked collections yet."
        self.canvas.set_map(built)
        self.lists.setTabText(0, "Passages" if built.passages else "Documents")
        self.theme_list.clear()
        dark = _is_dark(self.palette().base().color())
        for t in built.themes:
            pix = QPixmap(14, 14)
            pix.fill(QColor(PALETTE[t.color][1 if dark else 0]))
            item = QListWidgetItem(QIcon(pix), f"{t.label}  ({t.size:,})")
            item.setToolTip(f"{t.label}: {t.size:,} {what}" + (f", mostly in {t.folder}" if t.folder else ""))
            item.setData(ROLE, t.index)
            self.theme_list.addItem(item)
        if not len(built):
            self.message.setText(f"No indexed {what} in the ticked collections yet.")
        elif built.passages:
            sample = (f" (an even sample of {built.total:,})" if built.total > len(built) else "")
            self.message.setText(f"{len(built):,} passages{sample} in {len(built.themes)} themes. Lines join the "
                                 "closest passages in different documents. Click a dot to see its matches.")
        else:
            self.message.setText(f"{len(built):,} documents in {len(built.themes)} themes. " + DOCS_HINT)
        self._cleared()

    def shutdown(self) -> None:
        self._cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._links_pool.shutdown(wait=False, cancel_futures=True)

    # ---- selection -------------------------------------------------------------------
    def _pick_theme(self, t: int) -> None:
        self.canvas.select_theme(t)
        self._theme_selected(t)

    def _theme_selected(self, t: int) -> None:
        theme = self.map.themes[t]
        self._select_row(self.theme_list, t)
        what = "passages" if self.map.passages else "documents"
        folder = f" Most are in {theme.folder}." if theme.folder else ""
        self.detail.setText(f"<b>{_esc(theme.label)}</b>: {theme.size:,} {what}, most typical first.{folder}")
        self._fill_docs(self.map.members(t)[:LIST_LIMIT])
        self._clear_links()
        self.lists.setCurrentIndex(0)
        self._update_buttons()

    def _doc_selected(self, i: int) -> None:
        m = self.map
        self._select_row(self.theme_list, int(m.theme[i]))
        theme = m.themes[int(m.theme[i])]
        if m.passages:
            self.detail.setText(f"<b>{_esc(os.path.basename(m.paths[i]))}</b> · {_esc(m.locations[i])}<br>"
                                f"{_esc(_short(m.texts[i], 300))}<br>Theme: {_esc(theme.label)}")
            self._fill_docs(m.near(i, LIST_LIMIT), current=i)
            self._show_neighbours(i)
        else:
            self.detail.setText(f"<b>{_esc(os.path.basename(m.paths[i]))}</b><br>{_esc(m.paths[i])}<br>"
                                f"Theme: {_esc(theme.label)}.")
            self._fill_docs(m.near(i, LIST_LIMIT), current=i)
            self._request_links(i)
        self._update_buttons()

    def _cleared(self) -> None:
        self.theme_list.clearSelection()
        self.doc_list.blockSignals(True)  # clearing moves the current row, which would pick a dot
        self.doc_list.clear()
        self.doc_list.blockSignals(False)
        self._clear_links()
        what = "passage" if self.map.passages else "document"
        self.detail.setText(f"Pick a theme or a {what} to list what's in it." if len(self.map) else "")
        self._update_buttons()

    def _fill_docs(self, indices, current: int | None = None) -> None:
        self.doc_list.blockSignals(True)
        self.doc_list.clear()
        for i in indices:
            i = int(i)
            name = os.path.basename(self.map.paths[i])
            item = QListWidgetItem(f"{name} · {self.map.locations[i]}" if self.map.passages else name)
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
        if self.map.passages:
            self._show_neighbours(i)
        else:
            self.canvas.set_lines([])
            self._request_links(i)
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

    # ---- connections ---------------------------------------------------------------------
    def _clear_links(self) -> None:
        self._links_generation += 1  # drop links still being worked out
        self.links_tree.blockSignals(True)
        self.links_tree.clear()
        self.links_tree.blockSignals(False)
        self.lists.setTabText(1, "Connections")
        self.compare.hide()

    def _request_links(self, i: int) -> None:
        """Document view: work out which passages of document i match passages elsewhere."""
        self._clear_links()
        self._links_generation += 1
        generation, path = self._links_generation, self.map.paths[i]
        self.lists.setTabText(1, "Connections …")

        def work():
            try:
                links = self.engine.passage_links(path)
            except Exception as exc:  # NotAllowed, NothingToCompare, NotReady: shown in the list
                self.bridge.links.emit(generation, str(exc))
            else:
                self.bridge.links.emit(generation, links)

        self._links_pool.submit(work)

    def _links_ready(self, generation: int, links) -> None:
        if generation != self._links_generation or self.canvas.selected_doc is None:
            return
        source = self.canvas.selected_doc
        if isinstance(links, str):
            self.links_tree.addTopLevelItem(QTreeWidgetItem([links, ""]))
            self.lists.setTabText(1, "Connections")
            return
        count = 0
        for p in links.passages:
            if not p.links:
                continue
            where = f"{p.location} · {p.heading}" if p.heading else p.location
            parent = QTreeWidgetItem([f"{where}\n{_short(p.text, 140)}", ""])
            parent.setData(0, ROLE, ("source", p.text, where))
            parent.setToolTip(0, p.text)
            for link in p.links:
                same = "  · same text" if link.same_text else ""
                child = QTreeWidgetItem([f"{os.path.basename(link.path)} · {link.location}{same}",
                                         f"{link.similarity:.2f}"])
                child.setData(0, ROLE, ("link", link, p.text, where))
                child.setToolTip(0, f"{link.path}\n\n{link.text}")
                parent.addChild(child)
                count += 1
            self.links_tree.addTopLevelItem(parent)
            parent.setExpanded(True)
        if not count:
            self.links_tree.addTopLevelItem(QTreeWidgetItem(["No passage here closely matches another document.", ""]))
        self.lists.setTabText(1, f"Connections ({count})")
        lines = [(source, self._index[path], sim) for path, sim in links.linked_paths().items() if path in self._index]
        self.canvas.set_lines(lines)

    def _show_neighbours(self, i: int) -> None:
        """Passage view: the passage's closest passages in other documents, from the map itself."""
        self._clear_links()
        m = self.map
        lines = []
        for j, sim in zip(m.neighbours[i], m.neighbour_sim[i]):
            if j < 0:
                continue
            j = int(j)
            same = "  · same text" if sim >= 0.97 else ""
            item = QTreeWidgetItem([f"{os.path.basename(m.paths[j])} · {m.locations[j]}{same}\n"
                                    f"{_short(m.texts[j], 140)}", f"{sim:.2f}"])
            item.setData(0, ROLE, ("passage", j))
            item.setToolTip(0, f"{m.paths[j]}\n\n{m.texts[j]}")
            self.links_tree.addTopLevelItem(item)
            lines.append((i, j, float(sim)))
        self.lists.setTabText(1, f"Connections ({len(lines)})")
        self.canvas.set_lines(lines)

    def _link_picked(self, item, _previous=None) -> None:
        data = item.data(0, ROLE) if item else None
        source = self.canvas.selected_doc
        if not data or source is None or data[0] == "source":
            self.compare.hide()
            return
        m = self.map
        if data[0] == "passage":
            j = data[1]
            left = (os.path.basename(m.paths[source]), m.locations[source], m.texts[source])
            right = (os.path.basename(m.paths[j]), m.locations[j], m.texts[j])
            self.canvas.set_lines(self.canvas.lines, (source, j))
        else:
            _, link, text, where = data
            left = (os.path.basename(m.paths[source]), where, text)
            right = (os.path.basename(link.path), link.location, link.text)
            j = self._index.get(link.path)
            self.canvas.set_lines(self.canvas.lines, (source, j) if j is not None else None)
        cells = "".join(f"<td width='50%' valign='top'><b>{_esc(name)}</b> · {_esc(loc)}<br>{_esc(text)}</td>"
                        for name, loc, text in (left, right))
        self.compare.setHtml(f"<table width='100%' cellspacing='6'><tr>{cells}</tr></table>")
        self.compare.show()

    def _open_link(self, item, _column=0) -> None:
        data = item.data(0, ROLE)
        if not data:
            return
        if data[0] == "passage":
            self._open_index(data[1])
        elif data[0] == "link":
            QDesktopServices.openUrl(QUrl.fromLocalFile(data[1].path))

    # ---- actions -------------------------------------------------------------------------
    def _open_index(self, i: int | None) -> None:
        if i is not None and 0 <= i < len(self.map):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.map.paths[i]))

    def _find_similar(self) -> None:
        i = self.canvas.selected_doc
        if i is not None and self.show_similar:
            self.show_similar(self.map.paths[i])


DOCS_HINT = ("Click a dot or a theme, double-click a dot to open it. Picking a document shows which of its "
             "passages match other documents. Drag to move, scroll to zoom.")

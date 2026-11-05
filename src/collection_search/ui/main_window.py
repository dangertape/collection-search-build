"""Main window: Search, Similar, Collections, Tools and Status tabs."""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import time

from PySide6.QtCore import QObject, QPoint, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import claude_config
from ..addons import ToolSpec
from ..collections import Collection
from ..engine import Engine, NotReady, Status
from ..search import NotAllowed, NothingToCompare
from ..settings import SCOPE, TOOLS, Settings
from .collection_dialog import CollectionDialog
from .icon import app_icon

ID_ROLE = Qt.UserRole
TICKED, EVERYTHING = "", "*"  # collection picker choices besides single collections
PAGE_SIZE = 20  # search results shown at a time
SNIPPET = 400  # characters of a passage shown in the results list


class Bridge(QObject):
    """Carries events from worker threads to the UI thread (queued signals)."""

    status = Signal(object)
    settings_changed = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, engine: Engine, tools: list[ToolSpec], server_url: str = ""):
        super().__init__()
        self.settings, self.engine, self.tools = settings, engine, tools
        self.server_url = server_url
        self.quitting = False
        self.setWindowTitle("Collection Search")
        self.setWindowIcon(app_icon())
        self.resize(720, 520)

        self.bridge = Bridge()
        self.bridge.status.connect(self._show_status)
        self.bridge.settings_changed.connect(self._settings_changed)
        engine.on_status(self.bridge.status.emit)
        settings.subscribe(self.bridge.settings_changed.emit)

        self._pickers: list[QComboBox] = []
        self._search_offset = 0
        self._search_scope: list[Collection] | None = None
        self.tabs = QTabWidget()
        self.search_page = self._search_tab()
        self.similar_page = self._similar_tab()
        self.tabs.addTab(self.search_page, "Search")
        self.tabs.addTab(self.similar_page, "Similar")
        self.tabs.addTab(self._collections_tab(), "Collections")
        self.tabs.addTab(self._tools_tab(), "Tools")
        self.tabs.addTab(self._status_tab(), "Status")
        self.setCentralWidget(self.tabs)

        self.refresh_collections()
        self.refresh_tools()
        self._show_status(engine.status())

    # ---- Collections ----------------------------------------------------------------
    def _collections_tab(self) -> QWidget:
        self.collection_list = QListWidget()
        self.collection_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.collection_list.itemChanged.connect(self._collection_toggled)
        self.collection_list.itemDoubleClicked.connect(lambda _: self.edit_collection())
        add, edit, delete = QPushButton("Add…"), QPushButton("Edit…"), QPushButton("Delete")
        add.clicked.connect(self.add_collection)
        edit.clicked.connect(self.edit_collection)
        delete.clicked.connect(self.delete_collection)
        buttons = QHBoxLayout()
        for b in (add, edit, delete):
            buttons.addWidget(b)
        buttons.addStretch()
        hint = QLabel("Claude can only search collections that are ticked. Changes apply on Claude's next call.")
        hint.setWordWrap(True)
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(hint)
        layout.addWidget(self.collection_list)
        layout.addLayout(buttons)
        return tab

    def refresh_collections(self) -> None:
        selected = self._selected_collection_id()
        self.collection_list.blockSignals(True)
        self.collection_list.clear()
        for c in self.settings.collections():
            item = QListWidgetItem(f"{c.name}    —    {', '.join(c.folders)}")
            item.setData(ID_ROLE, c.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if c.active else Qt.Unchecked)
            self.collection_list.addItem(item)
            if c.id == selected:
                item.setSelected(True)
        self.collection_list.blockSignals(False)

    def _selected_collection_id(self) -> str | None:
        items = self.collection_list.selectedItems() if hasattr(self, "collection_list") else []
        return items[0].data(ID_ROLE) if items else None

    def _collection_toggled(self, item: QListWidgetItem) -> None:
        self.settings.set_collection_active(item.data(ID_ROLE), item.checkState() == Qt.Checked)

    def add_collection(self) -> None:
        names = {c.name for c in self.settings.collections()}
        result = _run_dialog(CollectionDialog(existing_names=names, parent=self))
        if result:
            self.settings.upsert_collection(result)

    def edit_collection(self) -> None:
        cid = self._selected_collection_id()
        collection = self.settings.get_collection(cid) if cid else None
        if not collection:
            return
        names = {c.name for c in self.settings.collections() if c.id != cid}
        result = _run_dialog(CollectionDialog(collection, names, parent=self))
        if result:
            self.settings.upsert_collection(result)

    def delete_collection(self) -> None:
        cid = self._selected_collection_id()
        collection = self.settings.get_collection(cid) if cid else None
        if not collection:
            return
        answer = QMessageBox.question(
            self, "Delete collection",
            f"Delete {collection.name}? Your files are not touched; they just stop being searchable.",
        )
        if answer == QMessageBox.Yes:
            self.settings.delete_collection(cid)

    # ---- collection picker (Search and Similar tabs) -----------------------------------
    def _scope_picker(self) -> QComboBox:
        combo = QComboBox()
        combo.setToolTip("Which collections to look in. Here you can search any collection, "
                         "ticked or not; Claude only ever sees the ticked ones.")
        combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._pickers.append(combo)
        self._fill_picker(combo)
        return combo

    def _fill_picker(self, combo: QComboBox) -> None:
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Ticked collections", TICKED)
        combo.addItem("All collections", EVERYTHING)
        for c in self.settings.collections():
            combo.addItem(c.name if c.active else f"{c.name} (not ticked)", c.id)
        index = combo.findData(current) if current is not None else 0
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def _picked(self, combo: QComboBox) -> list[Collection] | None:
        """The collections a picker points at; None means the ticked ones."""
        choice = combo.currentData()
        if choice in (None, TICKED):
            return None
        if choice == EVERYTHING:
            return self.settings.collections()
        collection = self.settings.get_collection(choice)
        return [collection] if collection else []

    # ---- Search -----------------------------------------------------------------------
    def _search_tab(self) -> QWidget:
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search your documents: a question, a topic or exact words")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.returnPressed.connect(self.run_search)
        self.search_scope = self._scope_picker()
        go = QPushButton("Search")
        go.clicked.connect(self.run_search)
        row = QHBoxLayout()
        row.addWidget(self.search_box, 1)
        row.addWidget(self.search_scope)
        row.addWidget(go)
        self.search_message = QLabel("Results are ranked by meaning and by exact words. "
                                     "Double-click a result to open it, or right-click for more.")
        self.search_message.setWordWrap(True)
        self.search_results = self._results_tree(["Passage", "Score", "Collections"])
        self.search_results.setRootIsDecorated(False)
        self.search_results.itemSelectionChanged.connect(self._search_selection_changed)
        self.search_open = QPushButton("Open")
        self.search_open.clicked.connect(lambda: self._open_selected(self.search_results))
        self.search_similar = QPushButton("Find similar")
        self.search_similar.setToolTip("Documents most like the selected one, on the Similar tab")
        self.search_similar.clicked.connect(self._similar_from_search)
        self.search_more = QPushButton("More results")
        self.search_more.clicked.connect(lambda: self.run_search(more=True))
        self.search_more.hide()
        bottom = QHBoxLayout()
        bottom.addWidget(self.search_open)
        bottom.addWidget(self.search_similar)
        bottom.addStretch()
        bottom.addWidget(self.search_more)
        self._search_selection_changed()
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addLayout(row)
        layout.addWidget(self.search_message)
        layout.addWidget(self.search_results)
        layout.addLayout(bottom)
        return tab

    def run_search(self, more: bool = False) -> None:
        if more:
            query, scope = self._search_query, self._search_scope
        else:
            query, scope = self.search_box.text().strip(), self._picked(self.search_scope)
            self._search_query, self._search_scope, self._search_offset = query, scope, 0
            self.search_results.clear()
            self.search_more.hide()
        if not query:
            return
        if scope == [] or (scope is None and not self.settings.active_collections()):
            self.search_message.setText("No collections to search. Tick one on the Collections tab, "
                                        "or pick a collection next to the search box.")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            results, has_more = self.engine.search_page(query, PAGE_SIZE, self._search_offset, collections=scope)
        except NotReady as exc:
            self.search_message.setText(str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._search_offset += len(results)
        for r in results:
            self.search_results.addTopLevelItem(_result_item(r))
        shown = self.search_results.topLevelItemCount()
        if not shown:
            self.search_message.setText(f"Nothing found for \"{query}\".")
        else:
            self.search_message.setText(f"{shown}{'+' if has_more else ''} passages for \"{query}\". "
                                        "Double-click a result to open it, or right-click for more.")
        self.search_more.setVisible(has_more)
        if not more and shown:
            self.search_results.setCurrentItem(self.search_results.topLevelItem(0))

    def _search_selection_changed(self) -> None:
        picked = bool(_selected_path(self.search_results))
        self.search_open.setEnabled(picked)
        self.search_similar.setEnabled(picked)

    def _similar_from_search(self) -> None:
        path = _selected_path(self.search_results)
        if not path:
            return
        index = self.similar_scope.findData(self.search_scope.currentData())
        self.similar_scope.setCurrentIndex(max(0, index))
        self.show_similar(path)

    def show_similar(self, path: str) -> None:
        """Switch to the Similar tab and list documents like `path`."""
        self.tabs.setCurrentWidget(self.similar_page)
        self.similar_path.setText(path)
        self.find_similar()

    # ---- results lists (Search and Similar tabs) ---------------------------------------
    def _results_tree(self, headers: list[str]) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setHeaderLabels(headers)
        tree.setWordWrap(True)
        tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        tree.header().setStretchLastSection(False)
        tree.itemDoubleClicked.connect(self._open_similar)
        tree.setContextMenuPolicy(Qt.CustomContextMenu)
        tree.customContextMenuRequested.connect(lambda pos: self._result_menu(tree, pos))
        return tree

    def _result_menu(self, tree: QTreeWidget, pos: QPoint) -> None:
        item = tree.itemAt(pos)
        path = item.data(0, ID_ROLE) if item else None
        if not path:
            return
        menu = QMenu(tree)
        menu.addAction("Open", lambda: _open_path(path))
        menu.addAction("Find similar", lambda: self._similar_from_menu(tree, path))
        menu.addAction("Show in folder", lambda: _show_in_folder(path))
        menu.addAction("Copy path", lambda: QGuiApplication.clipboard().setText(path))
        menu.exec(tree.viewport().mapToGlobal(pos))

    def _similar_from_menu(self, tree: QTreeWidget, path: str) -> None:
        if tree is self.search_results:
            index = self.similar_scope.findData(self.search_scope.currentData())
            self.similar_scope.setCurrentIndex(max(0, index))
        self.show_similar(path)

    def _open_selected(self, tree: QTreeWidget) -> None:
        path = _selected_path(tree)
        if path:
            _open_path(path)

    # ---- Similar ----------------------------------------------------------------------
    def _similar_tab(self) -> QWidget:
        self.similar_path = QLineEdit()
        self.similar_path.setPlaceholderText("Path of a document in a ticked collection")
        self.similar_path.returnPressed.connect(self.find_similar)
        self.similar_scope = self._scope_picker()
        browse, find = QPushButton("Browse…"), QPushButton("Find similar")
        browse.clicked.connect(self._browse_similar)
        find.clicked.connect(self.find_similar)
        row = QHBoxLayout()
        row.addWidget(self.similar_path, 1)
        row.addWidget(browse)
        row.addWidget(self.similar_scope)
        row.addWidget(find)
        self.similar_message = QLabel("Documents most like the one you pick, with the passages that match. "
                                      "Double-click a result to open it.")
        self.similar_message.setWordWrap(True)
        self.similar_results = self._results_tree(["Document", "Score", "Collections"])
        again = QPushButton("Find similar to selected")
        again.clicked.connect(self._similar_to_selected)
        bottom = QHBoxLayout()
        bottom.addWidget(again)
        bottom.addStretch()
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addLayout(row)
        layout.addWidget(self.similar_message)
        layout.addWidget(self.similar_results)
        layout.addLayout(bottom)
        return tab

    def _browse_similar(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Pick a document", os.path.dirname(self.similar_path.text()))
        if path:
            self.similar_path.setText(os.path.normpath(path))
            self.find_similar()

    def find_similar(self) -> None:
        path = self.similar_path.text().strip().strip('"')
        self.similar_results.clear()
        if not path:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = self.engine.find_similar(path, self.settings.preferences.max_result_count, passages=3,
                                              collections=self._picked(self.similar_scope))
        except (NotReady, NotAllowed, NothingToCompare) as exc:
            self.similar_message.setText(str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        if not result.results:
            self.similar_message.setText("No other documents in these collections to compare with.")
            return
        self.similar_message.setText(f"{len(result.results)} documents most like {os.path.basename(result.source)}. "
                                     "Double-click a result to open it.")
        for r in result.results:
            item = QTreeWidgetItem([f"{os.path.basename(r.path)}\n{r.path}", f"{r.score:.2f}",
                                    ", ".join(r.collections)])
            item.setData(0, ID_ROLE, r.path)
            item.setToolTip(0, f"Whole document {r.document_similarity:.2f}, best passages {r.passage_similarity:.2f}")
            for p in r.passages:
                child = QTreeWidgetItem([f"{p.location}  ↔  {p.source_location} of the source\n{p.text}",
                                         f"{p.similarity:.2f}", ""])
                child.setData(0, ID_ROLE, r.path)
                item.addChild(child)
            self.similar_results.addTopLevelItem(item)

    def _open_similar(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        path = item.data(0, ID_ROLE)
        if path:
            _open_path(path)

    def _similar_to_selected(self) -> None:
        items = self.similar_results.selectedItems()
        if items and items[0].data(0, ID_ROLE):
            self.similar_path.setText(items[0].data(0, ID_ROLE))
            self.find_similar()

    # ---- Tools ------------------------------------------------------------------------
    def _tools_tab(self) -> QWidget:
        self.tool_list = QListWidget()
        self.tool_list.setWordWrap(True)
        self.tool_list.itemChanged.connect(
            lambda item: self.settings.set_tool_enabled(item.data(ID_ROLE), item.checkState() == Qt.Checked)
        )
        hint = QLabel(
            "Claude always sees these tools. A tool that is off answers with a short error, starting with "
            "Claude's next call. New add-on tools appear after restarting the app and opening a new chat."
        )
        hint.setWordWrap(True)
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(hint)
        layout.addWidget(self.tool_list)
        return tab

    def refresh_tools(self) -> None:
        self.tool_list.blockSignals(True)
        self.tool_list.clear()
        for spec in self.tools:
            source = "" if spec.source == "built-in" else f"\n(add-on: {spec.source})"
            item = QListWidgetItem(f"{spec.name}\n{' '.join(spec.description.split())}{source}")
            item.setData(ID_ROLE, spec.name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if self.settings.tool_enabled(spec.name) else Qt.Unchecked)
            self.tool_list.addItem(item)
        self.tool_list.blockSignals(False)

    # ---- Status ------------------------------------------------------------------------
    def _status_tab(self) -> QWidget:
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.counts_label = QLabel()
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.errors = QPlainTextEdit()
        self.errors.setReadOnly(True)
        self.errors.setPlaceholderText("Files that could not be read appear here.")
        reindex = QPushButton("Reindex everything")
        reindex.clicked.connect(self._reindex)
        rescan = QPushButton("Check folders now")
        rescan.clicked.connect(self.engine.request_sync)
        row = QHBoxLayout()
        row.addWidget(rescan)
        row.addWidget(reindex)
        row.addStretch()

        index_box = QGroupBox("Index")
        index_layout = QVBoxLayout(index_box)
        index_layout.addWidget(self.status_label)
        index_layout.addWidget(self.progress)
        index_layout.addWidget(self.counts_label)
        index_layout.addWidget(QLabel("Problems"))
        index_layout.addWidget(self.errors)
        index_layout.addLayout(row)

        claude_box = QGroupBox("Connect Claude Desktop")
        self.claude_state = QLabel()
        self.claude_state.setWordWrap(True)
        snippet = QPlainTextEdit(claude_config.config_snippet())
        snippet.setReadOnly(True)
        snippet.setMaximumHeight(110)
        connect = QPushButton("Add to Claude Desktop")
        connect.clicked.connect(self._connect_claude)
        copy = QPushButton("Copy config")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(claude_config.config_snippet()))
        crow = QHBoxLayout()
        crow.addWidget(connect)
        crow.addWidget(copy)
        crow.addStretch()
        claude_layout = QVBoxLayout(claude_box)
        claude_layout.addWidget(self.claude_state)
        claude_layout.addWidget(snippet)
        claude_layout.addLayout(crow)
        self._refresh_claude_state()

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(index_box)
        layout.addWidget(claude_box)
        return tab

    def _show_status(self, status: Status) -> None:
        self.status_label.setText(status.message)
        if status.state == "indexing" and status.total:
            self.progress.setRange(0, status.total)
            self.progress.setValue(status.done)
            self.progress.setFormat(f"%v of %m   {os.path.basename(status.current)}")
            self.progress.show()
        elif status.state in ("loading model", "starting") or (status.state == "indexing" and not status.total):
            self.progress.setRange(0, 0)  # busy indicator
            self.progress.show()
        else:
            self.progress.hide()
        synced = time.strftime("%H:%M", time.localtime(status.last_sync)) if status.last_sync else "not yet"
        self.counts_label.setText(f"{status.files} files, {status.chunks} passages indexed. Last check: {synced}.")
        self.errors.setPlainText("\n".join(f"{os.path.basename(p)}: {e}    ({p})" for p, e in status.errors))

    def _reindex(self) -> None:
        answer = QMessageBox.question(self, "Reindex", "Rebuild the whole index? Search stays available but "
                                      "returns fewer results until it finishes.")
        if answer == QMessageBox.Yes:
            self.engine.reindex_all()

    def _refresh_claude_state(self) -> None:
        if claude_config.is_installed():
            text = "Claude Desktop is set up. Restart Claude Desktop if you just connected it."
        elif claude_config.has_old_entry():
            text = ("This version shows up in Claude as \"my-documents\". Click Add to Claude Desktop to "
                    "update the connection, then quit and reopen Claude Desktop.")
        else:
            text = ("Not connected yet. Click Add to Claude Desktop, then restart Claude Desktop. "
                    "Or paste this into its config file yourself:")
        self.claude_state.setText(text)

    def _connect_claude(self) -> None:
        try:
            path = claude_config.install()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not update Claude's config", str(exc))
            return
        self._refresh_claude_state()
        QMessageBox.information(self, "Connected", f"Added Collection Search to {path} as \"{claude_config.SERVER_KEY}\".\n\n"
                                "Quit and reopen Claude Desktop to load it.")

    # ---- events -------------------------------------------------------------------------
    def _settings_changed(self, kind: str) -> None:
        if kind == SCOPE:
            self.refresh_collections()
            for combo in self._pickers:
                self._fill_picker(combo)
        elif kind == TOOLS:
            self.refresh_tools()

    def closeEvent(self, event) -> None:
        if self.quitting:
            event.accept()
        else:
            event.ignore()  # closing the window hides it; Quit is in the tray menu
            self.hide()


def _result_item(r) -> QTreeWidgetItem:
    """One search result: file name and where in it, then the start of the passage."""
    where = r.heading or r.location
    text = " ".join(r.text.split())
    snippet = text if len(text) <= SNIPPET else text[:SNIPPET].rsplit(" ", 1)[0] + " …"
    item = QTreeWidgetItem([f"{os.path.basename(r.path)}  ·  {where}\n{snippet}", f"{r.score:.3f}",
                            ", ".join(r.collections)])
    item.setData(0, ID_ROLE, r.path)
    item.setToolTip(0, f"{r.path}  ({r.location})\n\n{text[:1500]}")
    return item


def _selected_path(tree: QTreeWidget) -> str | None:
    items = tree.selectedItems()
    return items[0].data(0, ID_ROLE) if items else None


def _open_path(path: str) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def _show_in_folder(path: str) -> None:
    if sys.platform == "win32" and os.path.exists(path):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))


def _run_dialog(dialog: CollectionDialog):
    """Show the dialog and return its collection (None if cancelled), then dispose of it here.

    A closed dialog's widgets form reference cycles that only the garbage collector
    frees. Left alone, a collection can run on the indexer thread, which destroys Qt
    widgets off the UI thread and crashes the app. So collect them now, on this thread.
    """
    result = dialog.result_collection() if dialog.exec() else None
    dialog.setParent(None)
    dialog.deleteLater()
    del dialog
    gc.collect()
    return result

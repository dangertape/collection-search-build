"""Main window: Collections, Tools and Status tabs."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from .. import claude_config
from ..addons import ToolSpec
from ..engine import Engine, Status
from ..settings import SCOPE, TOOLS, Settings
from .collection_dialog import CollectionDialog
from .icon import app_icon

ID_ROLE = Qt.UserRole


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

        self.tabs = QTabWidget()
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
        dialog = CollectionDialog(existing_names=names, parent=self)
        if dialog.exec():
            self.settings.upsert_collection(dialog.result_collection())

    def edit_collection(self) -> None:
        cid = self._selected_collection_id()
        collection = self.settings.get_collection(cid) if cid else None
        if not collection:
            return
        names = {c.name for c in self.settings.collections() if c.id != cid}
        dialog = CollectionDialog(collection, names, parent=self)
        if dialog.exec():
            self.settings.upsert_collection(dialog.result_collection())

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
        QMessageBox.information(self, "Connected", f"Added Collection Search to {path}.\n\n"
                                "Quit and reopen Claude Desktop to load it.")

    # ---- events -------------------------------------------------------------------------
    def _settings_changed(self, kind: str) -> None:
        if kind == SCOPE:
            self.refresh_collections()
        elif kind == TOOLS:
            self.refresh_tools()

    def closeEvent(self, event) -> None:
        if self.quitting:
            event.accept()
        else:
            event.ignore()  # closing the window hides it; Quit is in the tray menu
            self.hide()

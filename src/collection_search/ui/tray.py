"""Tray icon: switch collections and tools without opening the window."""

from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..settings import SCOPE, TOOLS, Settings
from .icon import app_icon


class Tray(QSystemTrayIcon):
    def __init__(self, settings: Settings, tool_names: list[str], show_window, quit_app, bridge):
        super().__init__(app_icon())
        self.settings, self.tool_names = settings, tool_names
        self._show_window, self._quit = show_window, quit_app
        self.setToolTip("Collection Search")
        self.menu = QMenu()
        self.setContextMenu(self.menu)
        self.activated.connect(self._activated)
        bridge.settings_changed.connect(self._settings_changed)
        bridge.status.connect(self._status)
        self.rebuild()

    def rebuild(self) -> None:
        self.menu.clear()
        self.menu.addAction("Open Collection Search", self._show_window)
        self.menu.addSeparator()
        self.menu.addSection("Collections")
        collections = self.settings.collections()
        if not collections:
            empty = self.menu.addAction("No collections yet")
            empty.setEnabled(False)
        for c in collections:
            action = QAction(c.name, self.menu, checkable=True, checked=c.active)
            action.toggled.connect(lambda on, cid=c.id: self.settings.set_collection_active(cid, on))
            self.menu.addAction(action)
        tools = self.menu.addMenu("Tools")
        for name in self.tool_names:
            action = QAction(name, tools, checkable=True, checked=self.settings.tool_enabled(name))
            action.toggled.connect(lambda on, n=name: self.settings.set_tool_enabled(n, on))
            tools.addAction(action)
        self.menu.addSeparator()
        self.menu.addAction("Quit", self._quit)
        active = any(c.active for c in collections)
        self.setIcon(app_icon(active))

    def _settings_changed(self, kind: str) -> None:
        if kind in (SCOPE, TOOLS):
            self.rebuild()

    def _status(self, status) -> None:
        self.setToolTip(f"Collection Search: {status.message}")

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_window()

"""The Collections tab's tree: each collection opens like Explorer onto its folders and documents.

Every row has a checkbox. A collection's box is its on/off switch for Claude.
A folder's or document's box turns it, and everything inside it, on or off
within that collection. A box is half-filled (mixed) when some of what is
inside is switched the other way. Switches are saved in the settings at once
and apply at search time, so off documents stay indexed and come back
instantly.

The tree lists what the index holds, so it reads no folders. Rows are made
when their parent is opened, so a collection of 35,000 documents opens fast.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtWidgets import QHeaderView, QMenu, QTreeWidget, QTreeWidgetItem

from ..collections import Collection, _is_under
from ..store import path_key

KIND_ROLE = Qt.UserRole  # "collection", "folder" or "file"
ID_ROLE = Qt.UserRole + 1  # the collection's id
PATH_ROLE = Qt.UserRole + 2  # the folder's or file's path, as shown
KEY_ROLE = Qt.UserRole + 3  # the same path, normalised
CHECK = {"on": Qt.Checked, "off": Qt.Unchecked, "mixed": Qt.PartiallyChecked}


@dataclass
class Node:
    """A folder in a collection, with the indexed documents under it."""

    name: str
    path: str
    key: str
    folders: dict[str, "Node"] = field(default_factory=dict)  # by normalised name
    files: list[tuple[str, str]] = field(default_factory=list)  # (path, key)
    total: int = 0
    on: int = 0


def build_nodes(collection: Collection, files: dict[str, str]) -> list[Node]:
    """One node per folder of the collection, holding the indexed files it covers."""
    roots = [Node(folder, folder, path_key(folder)) for folder in collection.folders]
    # A file under two of the collection's folders (one inside the other) is listed under the nearer.
    by_depth = sorted(roots, key=lambda n: len(n.key), reverse=True)
    for key, path in files.items():
        if not collection.covers(key):
            continue
        root = next((r for r in by_depth if _is_under(key, r.key)), None)
        if root is None:
            continue
        on = collection.switched_on(key)
        parts = _relative_parts(path, root.path, key, root.key)
        node = root
        node.total += 1
        node.on += on
        folder_key = root.key
        for name in parts[:-1]:
            folder_key = os.path.join(folder_key, os.path.normcase(name))
            child = node.folders.get(os.path.normcase(name))
            if child is None:
                child = Node(name, os.path.join(node.path, name), folder_key)
                node.folders[os.path.normcase(name)] = child
            node = child
            node.total += 1
            node.on += on
        node.files.append((path, key))
    return roots


def _relative_parts(path: str, root_path: str, key: str, root_key: str) -> list[str]:
    """The names between a collection folder and a file, spelled as on disk."""
    depth = len(os.path.relpath(key, root_key).split(os.sep))
    parts = []
    for _ in range(depth):
        path, name = os.path.split(path)
        parts.append(name)
    return parts[::-1]


class CollectionTree(QTreeWidget):
    """Emits the user's clicks; the main window turns them into settings changes."""

    collection_toggled = Signal(str, bool)  # collection id, on
    switch_toggled = Signal(str, str, bool)  # collection id, path, on
    edit_requested = Signal(str)  # collection id
    remove_requested = Signal(str)  # collection id
    open_requested = Signal(str)  # file path
    show_in_folder = Signal(str)  # path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["Collection, folder or document", "Documents on"])
        self.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.header().setStretchLastSection(False)
        self.setUniformRowHeights(True)
        self._collections: list[Collection] = []
        self._files: dict[str, str] = {}
        self._nodes: dict[str, list[Node]] = {}
        self._index_ready = False
        self.itemChanged.connect(self._changed)
        self.itemExpanded.connect(self._fill)
        self.itemDoubleClicked.connect(self._double_clicked)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

    # ---- data -----------------------------------------------------------------------
    def set_files(self, files: dict[str, str] | None) -> None:
        """The indexed files ({normalised path: path}); None while the index isn't open yet."""
        self._index_ready = files is not None
        self._files = files or {}
        self.set_collections(self._collections)

    def set_collections(self, collections: list[Collection]) -> None:
        """Redraw, keeping which rows are open, which is selected and the scroll position."""
        self._collections = collections
        self._nodes = {c.id: build_nodes(c, self._files) for c in collections}
        opened = self._opened_rows()
        current = _row_id(self.currentItem()) if self.currentItem() else None
        scroll = self.verticalScrollBar().value()
        self.blockSignals(True)
        try:
            self.clear()
            for c in collections:
                self.addTopLevelItem(self._collection_item(c))
            for item in self._all_items():
                if _row_id(item) in opened:
                    self._fill(item)
                    item.setExpanded(True)
            if current:
                found = next((i for i in self._all_items() if _row_id(i) == current), None)
                if found:
                    self.setCurrentItem(found)
        finally:
            self.blockSignals(False)
        self.verticalScrollBar().setValue(scroll)

    def selected_collection_id(self) -> str | None:
        item = self.currentItem()
        return item.data(0, ID_ROLE) if item else None

    def select_collection(self, collection_id: str) -> None:
        for i in range(self.topLevelItemCount()):
            if self.topLevelItem(i).data(0, ID_ROLE) == collection_id:
                self.setCurrentItem(self.topLevelItem(i))

    def collection_item(self, collection_id: str) -> QTreeWidgetItem | None:
        return next((self.topLevelItem(i) for i in range(self.topLevelItemCount())
                     if self.topLevelItem(i).data(0, ID_ROLE) == collection_id), None)

    def find_row(self, path: str) -> QTreeWidgetItem | None:
        """The row of a folder or document that has been drawn (its parents are open)."""
        key = path_key(path)
        return next((i for i in self._all_items() if i.data(0, KEY_ROLE) == key), None)

    # ---- rows -----------------------------------------------------------------------
    def _collection_item(self, c: Collection) -> QTreeWidgetItem:
        roots = self._nodes[c.id]
        total, on = sum(r.total for r in roots), sum(r.on for r in roots)
        item = QTreeWidgetItem([c.name, _count(on, total, self._index_ready) if c.active else "off"])
        item.setData(0, KIND_ROLE, "collection")
        item.setData(0, ID_ROLE, c.id)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        state = "off" if not c.active else ("mixed" if c.has_switched_off() else "on")
        item.setCheckState(0, CHECK[state])
        item.setToolTip(0, "\n".join(c.folders))
        item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        if c.active and c.has_switched_off():
            item.setToolTip(1, "Some folders or documents in this collection are switched off")
        return item

    def _child_item(self, c: Collection, kind: str, name: str, path: str, key: str,
                    node: Node | None) -> QTreeWidgetItem:
        if node is not None:
            count = _count(node.on, node.total, self._index_ready)
        else:
            count = ""
        item = QTreeWidgetItem([name, count])
        item.setData(0, KIND_ROLE, kind)
        item.setData(0, ID_ROLE, c.id)
        item.setData(0, PATH_ROLE, path)
        item.setData(0, KEY_ROLE, key)
        item.setToolTip(0, path)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(0, CHECK[c.switch_state(key)])
        if node is not None and (node.folders or node.files):
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        if not c.active:  # the collection is off: show the saved switches, greyed
            grey = self.palette().color(QPalette.Disabled, QPalette.Text)
            item.setForeground(0, grey)
            item.setForeground(1, grey)
        return item

    def _fill(self, item: QTreeWidgetItem) -> None:
        """Make an opened row's children, the first time it is opened."""
        if item.childCount():
            return
        c = self._collection(item.data(0, ID_ROLE))
        if c is None:
            return
        was_blocked = self.blockSignals(True)
        try:
            if item.data(0, KIND_ROLE) == "collection":
                for root in self._nodes[c.id]:
                    item.addChild(self._child_item(c, "folder", root.path, root.path, root.key, root))
                if not self._index_ready:
                    item.addChild(_note("The index is still opening. Folders and documents appear when it is ready."))
            elif item.data(0, KIND_ROLE) == "folder":
                node = self._node(c, item.data(0, KEY_ROLE))
                if node is None:
                    return
                for sub in sorted(node.folders.values(), key=lambda n: n.name.lower()):
                    item.addChild(self._child_item(c, "folder", sub.name, sub.path, sub.key, sub))
                for path, key in sorted(node.files, key=lambda f: os.path.basename(f[0]).lower()):
                    item.addChild(self._child_item(c, "file", os.path.basename(path), path, key, None))
        finally:
            self.blockSignals(was_blocked)

    def _node(self, c: Collection, key: str) -> Node | None:
        for root in self._nodes.get(c.id, []):
            if not _is_under(key, root.key):
                continue
            node = root
            while node.key != key:
                node = next((n for n in node.folders.values() if _is_under(key, n.key)), None)
                if node is None:
                    break
            else:
                return node
        return None

    def _collection(self, collection_id: str) -> Collection | None:
        return next((c for c in self._collections if c.id == collection_id), None)

    # ---- clicks -----------------------------------------------------------------------
    def _changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0 or item.data(0, KIND_ROLE) is None:
            return
        cid = item.data(0, ID_ROLE)
        c = self._collection(cid)
        if c is None:
            return
        if item.data(0, KIND_ROLE) == "collection":
            # Clicking a half-filled collection switches it off; its folder switches are kept.
            on = not c.active
            if on == c.active:
                return
            emit = lambda: self.collection_toggled.emit(cid, on)  # noqa: E731
        else:
            # A half-filled folder turns fully on when clicked, like Explorer-style pickers.
            path, on = item.data(0, PATH_ROLE), item.checkState(0) != Qt.Unchecked
            emit = lambda: self.switch_toggled.emit(cid, path, on)  # noqa: E731
        # After the click has finished: the change redraws the tree, which deletes this row.
        QTimer.singleShot(0, emit)

    def _double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        kind = item.data(0, KIND_ROLE)
        if kind == "collection":
            self.edit_requested.emit(item.data(0, ID_ROLE))
        elif kind == "file":
            self.open_requested.emit(item.data(0, PATH_ROLE))

    def _menu(self, pos: QPoint) -> None:
        item = self.itemAt(pos)
        if item is None or item.data(0, KIND_ROLE) is None:
            return
        menu = QMenu(self)
        kind, cid = item.data(0, KIND_ROLE), item.data(0, ID_ROLE)
        if kind == "collection":
            menu.addAction("Edit…", lambda: self.edit_requested.emit(cid))
            c = self._collection(cid)
            if c and c.switches:
                menu.addAction("Switch every folder and document back on",
                               lambda: [self.switch_toggled.emit(cid, f, True) for f in c.folders])
            menu.addSeparator()
            menu.addAction("Remove collection…", lambda: self.remove_requested.emit(cid))
        else:
            path = item.data(0, PATH_ROLE)
            if kind == "file":
                menu.addAction("Open", lambda: self.open_requested.emit(path))
            menu.addAction("Show in folder", lambda: self.show_in_folder.emit(path))
            menu.addAction("Copy path", lambda: QGuiApplication.clipboard().setText(path))
        menu.exec(self.viewport().mapToGlobal(pos))

    def keyPressEvent(self, event) -> None:
        item = self.currentItem()
        if event.key() == Qt.Key_Delete and item is not None and item.data(0, KIND_ROLE) == "collection":
            self.remove_requested.emit(item.data(0, ID_ROLE))
            return
        super().keyPressEvent(event)

    # ---- helpers ----------------------------------------------------------------------
    def _all_items(self):
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop(0)
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _opened_rows(self) -> set:
        return {_row_id(i) for i in self._all_items() if i.isExpanded()}


def _row_id(item: QTreeWidgetItem):
    return (item.data(0, ID_ROLE), item.data(0, KEY_ROLE))


def _count(on: int, total: int, ready: bool) -> str:
    if not ready:
        return ""
    return f"{total}" if on == total else f"{on} of {total}"


def _note(text: str) -> QTreeWidgetItem:
    item = QTreeWidgetItem([text, ""])
    item.setFlags(Qt.ItemIsEnabled)
    return item

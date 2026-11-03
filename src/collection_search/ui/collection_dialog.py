"""Add or edit one collection: folders, exclusions, file types, dates, name patterns."""

from __future__ import annotations

import copy
import os
from datetime import date

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from ..collections import Collection
from ..parsers import SUPPORTED_EXTENSIONS


class FolderList(QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.list = QListWidget()
        self.list.setMinimumHeight(70)
        add, remove = QPushButton("Add folder…"), QPushButton("Remove")
        add.clicked.connect(self._add)
        remove.clicked.connect(self._remove)
        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(title))
        layout.addWidget(self.list)
        layout.addLayout(buttons)
        self.start_dir_provider = lambda: ""

    def folders(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def set_folders(self, folders: list[str]) -> None:
        self.list.clear()
        self.list.addItems(folders)

    def _add(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder", self.start_dir_provider())
        if folder:
            folder = os.path.normpath(folder)
            if folder not in self.folders():
                self.list.addItem(folder)

    def _remove(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))


class CollectionDialog(QDialog):
    def __init__(self, collection: Collection | None = None, existing_names: set[str] = frozenset(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit collection" if collection else "New collection")
        self.setMinimumWidth(560)
        self._original = collection
        self._existing_names = existing_names

        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Client work")
        self.folders = FolderList("Folders to include (subfolders included)")
        self.excluded = FolderList("Subfolders to skip")
        self.excluded.start_dir_provider = lambda: (self.folders.folders() or [""])[0]

        self.type_boxes: dict[str, QCheckBox] = {}
        types = QGridLayout()
        for i, ext in enumerate(sorted(SUPPORTED_EXTENSIONS)):
            box = QCheckBox(ext)
            box.setChecked(True)
            self.type_boxes[ext] = box
            types.addWidget(box, i // 5, i % 5)

        self.after_on, self.after = QCheckBox("Modified after"), _date_edit()
        self.before_on, self.before = QCheckBox("Modified before"), _date_edit()
        self.after_on.toggled.connect(self.after.setEnabled)
        self.before_on.toggled.connect(self.before.setEnabled)
        self.after.setEnabled(False)
        self.before.setEnabled(False)

        self.include = QLineEdit()
        self.include.setPlaceholderText("e.g. *contract*; *.pdf   (empty = all files)")
        self.exclude = QLineEdit()
        self.exclude.setPlaceholderText("e.g. *draft*; ~$*")

        filters = QFormLayout()
        filters.addRow("File types", _wrap(types))
        for box, edit in ((self.after_on, self.after), (self.before_on, self.before)):
            row = QHBoxLayout()
            row.addWidget(box)
            row.addWidget(edit)
            row.addStretch()
            filters.addRow("Dates" if box is self.after_on else "", _wrap(row))
        filters.addRow("Only names like", self.include)
        filters.addRow("Skip names like", self.exclude)
        filter_box = QGroupBox("Filters")
        filter_box.setLayout(filters)

        note = QLabel("Dates and name patterns apply instantly. Folder and type changes re-index in the background.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Name", self.name)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.folders)
        layout.addWidget(self.excluded)
        layout.addWidget(filter_box)
        layout.addWidget(note)
        layout.addWidget(buttons)

        if collection:
            self._load(collection)

    def _load(self, c: Collection) -> None:
        self.name.setText(c.name)
        self.folders.set_folders(c.folders)
        self.excluded.set_folders(c.excluded)
        exts = {e.lower() for e in c.extensions}
        for ext, box in self.type_boxes.items():
            box.setChecked(ext in exts)
        if c.modified_after:
            self.after_on.setChecked(True)
            self.after.setDate(QDate(c.modified_after.year, c.modified_after.month, c.modified_after.day))
        if c.modified_before:
            self.before_on.setChecked(True)
            self.before.setDate(QDate(c.modified_before.year, c.modified_before.month, c.modified_before.day))
        self.include.setText("; ".join(c.include_patterns))
        self.exclude.setText("; ".join(c.exclude_patterns))

    def result_collection(self) -> Collection:
        base = copy.deepcopy(self._original) if self._original else None
        values = dict(
            name=self.name.text().strip(),
            folders=self.folders.folders(),
            excluded=self.excluded.folders(),
            extensions=[e for e, box in self.type_boxes.items() if box.isChecked()],
            modified_after=_py_date(self.after) if self.after_on.isChecked() else None,
            modified_before=_py_date(self.before) if self.before_on.isChecked() else None,
            include_patterns=_patterns(self.include.text()),
            exclude_patterns=_patterns(self.exclude.text()),
        )
        if base:
            return Collection(**values, active=base.active, id=base.id)
        return Collection(**values)

    def _accept(self) -> None:
        problem = self._problem()
        if problem:
            QMessageBox.warning(self, "Check the collection", problem)
            return
        self.accept()

    def _problem(self) -> str | None:
        name = self.name.text().strip()
        if not name:
            return "Give the collection a name."
        if name in self._existing_names:
            return f"There is already a collection called {name}."
        if not self.folders.folders():
            return "Add at least one folder."
        if not any(box.isChecked() for box in self.type_boxes.values()):
            return "Pick at least one file type."
        if self.after_on.isChecked() and self.before_on.isChecked() and self.after.date() > self.before.date():
            return "The 'modified after' date is later than the 'modified before' date."
        return None


def _date_edit() -> QDateEdit:
    edit = QDateEdit(QDate.currentDate())
    edit.setCalendarPopup(True)
    edit.setDisplayFormat("yyyy-MM-dd")
    return edit


def _py_date(edit: QDateEdit) -> date:
    d = edit.date()
    return date(d.year(), d.month(), d.day())


def _patterns(text: str) -> list[str]:
    return [p.strip() for p in text.replace(",", ";").split(";") if p.strip()]


def _wrap(layout) -> QWidget:
    w = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    w.setLayout(layout)
    return w

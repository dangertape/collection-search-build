"""Smoke tests for the window and tray, run without a screen."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
from PySide6.QtCore import Qt  # noqa: E402

from collection_search.addons import load_tools  # noqa: E402
from collection_search.collections import Collection  # noqa: E402
from collection_search.embeddings import HashEmbedder  # noqa: E402
from collection_search.engine import Engine  # noqa: E402
from collection_search.settings import Settings  # noqa: E402

from conftest import write  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(qapp, tmp_path):
    from collection_search.ui.main_window import MainWindow
    from collection_search.ui.tray import Tray

    write(str(tmp_path / "docs" / "a.txt"), "hello")
    settings = Settings.load(str(tmp_path / "s.json"))
    settings.preferences.watch_folders = False
    settings.upsert_collection(Collection("Docs", [str(tmp_path / "docs")]))
    engine = Engine(settings, str(tmp_path / "i.db"), lambda m: HashEmbedder())
    tools = load_tools()
    w = MainWindow(settings, engine, tools)
    tray = Tray(settings, [t.name for t in tools], w.show, lambda: None, w.bridge)
    engine.start()
    yield w, tray, settings, engine, qapp
    engine.stop()


def pump(app, seconds=0.5):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def test_checkboxes_drive_settings(window):
    w, tray, settings, engine, app = window
    w.collection_list.item(0).setCheckState(Qt.Unchecked)
    assert settings.active_collections() == []
    w.tool_list.item(0).setCheckState(Qt.Unchecked)
    assert not settings.tool_enabled(w.tool_list.item(0).data(Qt.UserRole))


def test_tray_and_window_stay_in_step(window):
    w, tray, settings, engine, app = window
    docs_action = next(a for a in tray.menu.actions() if a.text() == "Docs")
    docs_action.setChecked(False)
    pump(app, 0.2)
    assert settings.active_collections() == []
    assert w.collection_list.item(0).checkState() == Qt.Unchecked


def test_status_shows_index_counts(window):
    w, tray, settings, engine, app = window
    end = time.time() + 10
    while engine.status().state != "idle" and time.time() < end:
        pump(app, 0.1)
    pump(app, 0.2)
    assert "1 files" in w.counts_label.text()


def test_closing_hides_instead_of_quitting(window):
    w, *_ = window
    w.show()
    w.close()
    assert not w.isVisible()


def test_dialog_validation_and_result(qapp, tmp_path):
    from collection_search.ui.collection_dialog import CollectionDialog

    original = Collection("Docs", [str(tmp_path)], exclude_patterns=["*draft*"], active=False)
    d = CollectionDialog(original, existing_names={"Other"})
    result = d.result_collection()
    assert (result.id, result.active, result.exclude_patterns) == (original.id, False, ["*draft*"])
    d.name.setText("Other")
    assert "already" in d._problem()
    d.name.setText("New")
    for box in d.type_boxes.values():
        box.setChecked(False)
    assert "file type" in d._problem()


def test_similar_tab(window, tmp_path):
    w, tray, settings, engine, app = window
    write(str(tmp_path / "docs" / "b.txt"), "hello world")
    engine.request_sync()
    end = time.time() + 10
    while engine.status().files < 2 and time.time() < end:
        pump(app, 0.1)
    w.similar_path.setText(str(tmp_path / "docs" / "a.txt"))
    w.find_similar()
    assert w.similar_results.topLevelItemCount() == 1
    assert "b.txt" in w.similar_results.topLevelItem(0).text(0)
    w.similar_path.setText(str(tmp_path / "elsewhere.txt"))
    w.find_similar()
    assert w.similar_results.topLevelItemCount() == 0
    assert "not in an active collection" in w.similar_message.text()


def test_map_tab(window, tmp_path):
    w, tray, settings, engine, app = window
    write(str(tmp_path / "docs" / "b.txt"), "hello world")
    engine.request_sync()
    end = time.time() + 10
    while engine.status().files < 2 and time.time() < end:
        pump(app, 0.1)
    w.show()
    w.tabs.setCurrentWidget(w.map_tab)
    end = time.time() + 10
    while len(w.map_tab.map) < 2 and time.time() < end:
        pump(app, 0.1)
    tab = w.map_tab
    assert len(tab.map) == 2 and tab.theme_list.count() == len(tab.map.themes)
    tab.theme_list.setCurrentRow(0)
    tab._pick_theme(0)
    assert tab.doc_list.count() == tab.map.themes[0].size
    i = next(k for k, p in enumerate(tab.map.paths) if p.endswith("a.txt"))
    x, y = tab.canvas._screen[i]
    assert tab.canvas.doc_at(x, y) == i
    tab.canvas.select_doc(i)
    tab._doc_selected(i)
    assert tab.similar_button.isEnabled()
    tab._find_similar()
    assert w.tabs.currentIndex() == 1 and w.similar_path.text().endswith("a.txt")
    assert w.similar_results.topLevelItemCount() == 1
    # unticking the collection empties the map
    w.tabs.setCurrentWidget(w.map_tab)
    w.collection_list.item(0).setCheckState(Qt.Unchecked)
    end = time.time() + 10
    while len(tab.map) and time.time() < end:
        pump(app, 0.1)
    assert len(tab.map) == 0
    w.map_tab.shutdown()

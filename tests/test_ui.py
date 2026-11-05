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


def _wait_files(engine, app, n):
    end = time.time() + 10
    while (engine.status().files < n or engine.status().state != "idle") and time.time() < end:
        pump(app, 0.1)


def test_search_tab_and_find_similar(window, tmp_path):
    w, tray, settings, engine, app = window
    write(str(tmp_path / "docs" / "b.txt"), "hello world of apples")
    engine.request_sync()
    _wait_files(engine, app, 2)
    assert w.tabs.tabText(0) == "Search"
    w.search_box.setText("apples")
    w.run_search()
    assert w.search_results.topLevelItemCount() >= 1
    first = w.search_results.topLevelItem(0)
    assert "b.txt" in first.text(0) and "apples" in first.text(0)
    assert w.search_similar.isEnabled()
    w._similar_from_search()
    assert w.tabs.currentWidget() is w.similar_page
    assert w.similar_path.text().endswith("b.txt")
    assert "a.txt" in w.similar_results.topLevelItem(0).text(0)


def test_search_picker_reaches_unticked_collections(window, tmp_path):
    w, tray, settings, engine, app = window
    write(str(tmp_path / "private" / "p.txt"), "secret pancake recipe")
    private = Collection("Private", [str(tmp_path / "private")], active=False)
    settings.upsert_collection(private)
    pump(app, 0.2)
    _wait_files(engine, app, 2)
    labels = [w.search_scope.itemText(i) for i in range(w.search_scope.count())]
    assert "Private (not ticked)" in labels

    w.search_box.setText("pancake")
    w.search_scope.setCurrentIndex(w.search_scope.findData(""))  # ticked only, as Claude sees it
    w.run_search()
    assert all("p.txt" not in w.search_results.topLevelItem(i).text(0)
               for i in range(w.search_results.topLevelItemCount()))

    w.search_scope.setCurrentIndex(w.search_scope.findData(private.id))
    w.run_search()
    assert "p.txt" in w.search_results.topLevelItem(0).text(0)
    assert settings.active_collections()[0].name == "Docs"  # searching didn't tick it
    assert all("p.txt" not in r.path for r in engine.search("pancake", 10))  # Claude still can't see it

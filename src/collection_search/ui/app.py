"""Start the app: settings, engine, MCP server, window and tray, in one process.

Threads: Qt's main loop (UI), the indexer thread (engine), and the MCP
server thread. Settings are the only shared state.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from .. import paths
from ..addons import load_tools
from ..engine import Engine, default_embedder_factory
from ..mcp_server import HttpServer, build_server
from ..settings import Settings
from .foreground import allow_other_process_to_take_focus, bring_to_front, is_minimized
from .icon import app_icon
from .main_window import MainWindow
from .tray import Tray

INSTANCE_KEY = "CollectionSearch-instance"
log = logging.getLogger("collection_search")


def main(args: list[str]) -> int:
    _setup_logging()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Collection Search")
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)  # closing the window leaves the tray running

    lock = QLockFile(paths.lock_path())
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        if not _started_by_connector():  # the connector only wanted it running; it is
            _ask_running_instance_to_show()
        return 0

    settings = Settings.load(paths.settings_path())
    engine = Engine(settings, paths.index_path(), default_embedder_factory(paths.models_dir()))
    tools = load_tools(paths.addon_dirs())
    try:
        http = HttpServer(build_server(engine, tools), paths.connection_path())
    except OSError as exc:
        QMessageBox.critical(None, "Collection Search", f"Could not start the server for Claude: {exc}")
        return 1

    window = MainWindow(settings, engine, tools, http.url)
    stopped = False

    def show_window():
        bring_to_front(window)

    def quit_app():
        nonlocal stopped
        if stopped:
            return
        stopped = True
        window.quitting = True
        window.status_label.setText("Stopping…")
        http.stop()
        window.map_tab.shutdown()
        engine.stop()
        app.quit()

    tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = Tray(settings, [t.name for t in tools], show_window, quit_app, window.bridge)
        tray.show()
    else:
        window.quitting = True  # no tray: closing the window quits
    app.aboutToQuit.connect(quit_app)

    instance_server = QLocalServer()
    QLocalServer.removeServer(INSTANCE_KEY)
    instance_server.listen(INSTANCE_KEY)
    instance_server.newConnection.connect(lambda: (instance_server.nextPendingConnection(), show_window()))

    engine.start()
    http.start()
    log.info("Started; MCP server at %s", http.url)

    in_tray = "--tray" in args or _started_by_connector()
    if not in_tray or tray is None:
        show_window()
        # Again once the event loop runs, in case Windows kept it behind or started it minimized
        # (a launcher's "start minimized" setting applies to the first window, which is then never drawn).
        QTimer.singleShot(0, show_window)
        QTimer.singleShot(1000, lambda: is_minimized(window) and show_window())
    code = app.exec()
    lock.unlock()
    return code


def _started_by_connector() -> bool:
    """True when Claude's connector just started the app; it then stays in the tray."""
    from ..shim import LAUNCH_MARKER

    marker = os.path.join(paths.roaming_dir(), LAUNCH_MARKER)
    try:
        fresh = time.time() - os.path.getmtime(marker) < 120
        os.remove(marker)
    except OSError:
        return False
    return fresh


def _ask_running_instance_to_show() -> None:
    allow_other_process_to_take_focus()  # we were just launched, so we may hand the front to the running app
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_KEY)
    if not socket.waitForConnected(1000):
        QMessageBox.information(None, "Collection Search", "Collection Search is already running. "
                                "Open it from the tray icon.")
    socket.disconnectFromServer()


def _setup_logging() -> None:
    from ..mcp_server import harden_logging

    harden_logging()
    handler = logging.handlers.RotatingFileHandler(paths.log_path(), maxBytes=1_000_000, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:  # windowed builds have no console
        root.addHandler(logging.StreamHandler())

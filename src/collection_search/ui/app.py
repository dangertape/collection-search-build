"""Start the app: settings, engine, MCP server, window and tray, in one process.

Threads: Qt's main loop (UI), the indexer thread (engine), and the MCP
server thread. Settings are the only shared state.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

from PySide6.QtCore import QLockFile
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from .. import paths
from ..addons import load_tools
from ..engine import Engine, default_embedder_factory
from ..mcp_server import HttpServer, build_server
from ..settings import Settings
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
        window.show()
        window.raise_()
        window.activateWindow()

    def quit_app():
        nonlocal stopped
        if stopped:
            return
        stopped = True
        window.quitting = True
        window.status_label.setText("Stopping…")
        http.stop()
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

    if "--tray" not in args or tray is None:
        show_window()
    code = app.exec()
    lock.unlock()
    return code


def _ask_running_instance_to_show() -> None:
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_KEY)
    if not socket.waitForConnected(1000):
        QMessageBox.information(None, "Collection Search", "Collection Search is already running. "
                                "Open it from the tray icon.")
    socket.disconnectFromServer()


def _setup_logging() -> None:
    handler = logging.handlers.RotatingFileHandler(paths.log_path(), maxBytes=1_000_000, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:  # windowed builds have no console
        root.addHandler(logging.StreamHandler())

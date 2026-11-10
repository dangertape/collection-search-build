"""Bring the main window to the front, visibly, every time it is opened.

Windows only lets the program the user is working in move another window to
the front. The app takes a few seconds to start, so if the user clicks
anywhere in the meantime, a plain show() and activateWindow() leave the window
behind everything else, with only a taskbar button to show it opened. The same
happens when a second launch asks the running app to show itself. So on
Windows we also ask the system directly to put the window on top.

Windows also applies a launcher's "start minimized" request to the first
window a program shows, before it is ever drawn. That leaves a taskbar button
with a blank white preview and no window, so a minimized window is restored.
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)


def bring_to_front(window: QWidget) -> None:
    """Show the window restored, on a visible screen, above other windows and focused."""
    window.setWindowState((window.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
    window.show()
    keep_on_screen(window)
    window.raise_()
    window.activateWindow()
    if sys.platform == "win32":
        try:
            _force_foreground_win32(int(window.winId()))
        except Exception:  # never let this stop the window opening
            log.exception("Could not bring the window to the front")


def is_minimized(window: QWidget) -> bool:
    """Minimized as Windows sees it too: a launch can ask Windows to start the app minimized."""
    if window.isMinimized():
        return True
    if sys.platform == "win32" and window.isVisible():
        try:
            import ctypes

            return bool(ctypes.windll.user32.IsIconic(ctypes.c_void_p(int(window.winId()))))
        except Exception:
            return False
    return False


def keep_on_screen(window: QWidget) -> None:
    """Move the window onto the primary screen if it is (mostly) outside every screen."""
    frame = window.frameGeometry()
    visible = max((s.availableGeometry().intersected(frame) for s in QGuiApplication.screens()),
                  key=lambda r: r.width() * r.height(), default=None)
    if visible is not None and visible.width() >= 100 and visible.height() >= 50:
        return
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return
    area = screen.availableGeometry()
    if window.width() > area.width() or window.height() > area.height():
        window.resize(min(window.width(), area.width()), min(window.height(), area.height()))
        frame = window.frameGeometry()
    frame.moveCenter(area.center())
    window.move(frame.topLeft())


def allow_other_process_to_take_focus() -> None:
    """A second launch calls this before asking the running app to show its window."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except Exception:
            log.exception("AllowSetForegroundWindow failed")


def _force_foreground_win32(hwnd: int) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, wintypes.UINT]
    for name in ("ShowWindow", "IsIconic", "BringWindowToTop", "SetForegroundWindow"):
        getattr(user32, name).argtypes = [wintypes.HWND] + ([ctypes.c_int] if name == "ShowWindow" else [])

    SW_RESTORE, SW_SHOW = 9, 5
    HWND_TOPMOST, HWND_NOTOPMOST = wintypes.HWND(-1), wintypes.HWND(-2)
    SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040
    flags = SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW

    user32.ShowWindow(hwnd, SW_RESTORE if user32.IsIconic(hwnd) else SW_SHOW)

    # Windows accepts SetForegroundWindow from a thread that shares input with the
    # current foreground window, so join its input queue for the call.
    foreground = user32.GetForegroundWindow()
    this_thread = kernel32.GetCurrentThreadId()
    other_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    attached = bool(other_thread and other_thread != this_thread
                    and user32.AttachThreadInput(other_thread, this_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(other_thread, this_thread, False)

    # If focus was still refused, at least put the window above the others on screen.
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)

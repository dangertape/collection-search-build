# PyInstaller spec: one folder holding two programs.
#
#   CollectionSearch.exe         the app (window, tray, indexer, MCP server); no console
#   collection-search-mcp.exe    the stdio shim Claude Desktop launches; console program
#
# The shim is a separate console build because a windowed program may start
# without usable stdin/stdout, which the stdio connection needs.
#
# Build from the repo root:  pyinstaller packaging/collection_search.spec --noconfirm

import os
import sys

# Conda Pythons keep the DLLs that extension modules need (libexpat, ffi,
# ssl, sqlite...) in Library\bin, which is only on PATH in an activated
# shell. Put it on PATH so PyInstaller finds and bundles them; otherwise the
# app fails at start-up with "DLL load failed while importing pyexpat".
_conda_bin = os.path.join(sys.base_prefix, "Library", "bin")
if os.name == "nt" and os.path.isdir(_conda_bin):
    os.environ["PATH"] = _conda_bin + os.pathsep + os.environ.get("PATH", "")

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")

app_hidden = (
    collect_submodules("collection_search")  # includes built-in tools, found at runtime
    + collect_submodules("uvicorn")
    + collect_submodules("mcp", filter=lambda name: not name.startswith("mcp.cli"))  # cli needs typer
)
app_datas = collect_data_files("fastembed") + collect_data_files("sqlite_vec") + collect_data_files("mcp")
app_binaries = collect_dynamic_libs("sqlite_vec") + collect_dynamic_libs("onnxruntime")

bundled_model = os.path.join(ROOT, "models")  # optional: pre-downloaded model for offline installs
if os.path.isdir(bundled_model):
    app_datas.append((bundled_model, "models"))

app_a = Analysis(
    [os.path.join(SPECPATH, "app_entry.py")],
    pathex=[SRC],
    binaries=app_binaries,
    datas=app_datas,
    hiddenimports=app_hidden,
    excludes=["tkinter", "matplotlib", "torch", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore"],
    noarchive=False,
)
app_pyz = PYZ(app_a.pure)
app_exe = EXE(
    app_pyz,
    app_a.scripts,
    [],
    exclude_binaries=True,
    name="CollectionSearch",
    console=False,
    icon=os.path.join(SPECPATH, "app.ico") if os.path.exists(os.path.join(SPECPATH, "app.ico")) else None,
)

shim_a = Analysis(
    [os.path.join(SPECPATH, "shim_entry.py")],
    pathex=[SRC],
    excludes=["PySide6", "fastembed", "onnxruntime", "numpy", "mcp", "uvicorn", "pydantic", "tkinter"],
)
shim_pyz = PYZ(shim_a.pure)
shim_exe = EXE(
    shim_pyz,
    shim_a.scripts,
    [],
    exclude_binaries=True,
    name="collection-search-mcp",
    console=True,
)

coll = COLLECT(
    app_exe, app_a.binaries, app_a.datas,
    shim_exe, shim_a.binaries, shim_a.datas,
    name="CollectionSearch",
)

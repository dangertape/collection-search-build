# Build the Windows folder (and optionally the installer).
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-BundleModel] [-Installer]
param([switch]$BundleModel, [switch]$Installer)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path .venv)) { py -3.12 -m venv .venv }
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev,build]"
# Swap the CPU-only onnxruntime that fastembed pulls in for the DirectML build, which runs
# the search model on any DirectX 12 graphics card (Nvidia, AMD, Intel) and on the CPU
# otherwise. Both packages install the same "onnxruntime" module, so only one may be present.
.\.venv\Scripts\python -m pip uninstall -y onnxruntime onnxruntime-directml
.\.venv\Scripts\python -m pip install --no-deps onnxruntime-directml
.\.venv\Scripts\python -c "import onnxruntime as o; p = o.get_available_providers(); print('onnxruntime providers:', p); assert 'DmlExecutionProvider' in p"

if ($BundleModel) {
    # Download the embedding model into .\models so the app works offline from first run.
    .\.venv\Scripts\python -c "from fastembed import TextEmbedding; from collection_search.embeddings import DEFAULT_MODEL; TextEmbedding(DEFAULT_MODEL, cache_dir='models')"
}

.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\pyinstaller packaging\collection_search.spec --noconfirm --clean
Write-Host "Built dist\CollectionSearch\CollectionSearch.exe"

if ($Installer) {
    $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $iscc)) { throw "Inno Setup 6 not found at $iscc" }
    & $iscc packaging\installer.iss
    Write-Host "Built dist\CollectionSearch-Setup.exe"
}

# PyInstaller spec: single standalone owa-piggy binary (onefile).
#
# Run from packaging/: `pyinstaller owa-piggy.spec`. Produces dist/owa-piggy.
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

hidden = collect_submodules("owa_piggy")

# Bundle the dist metadata: __version__ falls back to
# importlib.metadata.version("owa-piggy") when the source pyproject.toml
# isn't reachable (which is the case inside a frozen binary).
datas = copy_metadata("owa-piggy")

a = Analysis(
    ["entry.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="owa-piggy",
    console=True,
    strip=False,
    upx=False,
)

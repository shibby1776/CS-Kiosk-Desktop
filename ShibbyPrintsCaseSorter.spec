# PyInstaller ONEDIR build: pyinstaller --clean --noconfirm ShibbyPrintsCaseSorter.spec
#
# PyTorch and OpenCV ship as normal files beside the executable. This avoids
# the expensive extraction step imposed by a one-file executable on every
# application launch. PyInstaller's maintained package hooks collect the
# required Torch, TorchVision and OpenCV native libraries.
from PyInstaller.utils.hooks import collect_all, collect_submodules

hiddenimports = collect_submodules("sorter")
# pygrabber discovers COM support dynamically on Windows.
hiddenimports += collect_submodules("pygrabber")
hiddenimports += collect_submodules("comtypes")

# Torch is imported lazily, so Analysis cannot discover it reliably. Explicitly
# collect its binaries/data/submodules. ONEDIR keeps these files beside the EXE,
# avoiding one-file extraction while guaranteeing local inference is present.
torch_datas, torch_binaries, torch_hidden = collect_all("torch")
tv_datas, tv_binaries, tv_hidden = collect_all("torchvision")
hiddenimports += torch_hidden + tv_hidden

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=torch_binaries + tv_binaries,
    datas=[
        ("assets", "assets"),
        ("build_report", "build_report"),
        ("Saved Bins Samples", "Saved Bins Samples"),
    ] + torch_datas + tv_datas,
    hiddenimports=hiddenimports,
    excludes=[
        "pytest",
        "setuptools.tests",
        "torch.testing._internal",
        # Raspberry Pi appliance controls do not belong in the Windows build.
        "sorter.hardware_manager",
        "sorter.ui.tab_wifi",
    ],
)
pyz = PYZ(a.pure)

# Excluding binaries and data here is what makes this an onedir executable.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ShibbyPrintsCaseSorter",
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ShibbyPrintsCaseSorter",
)

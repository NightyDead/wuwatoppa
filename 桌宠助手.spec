# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the desktop pet assistant.

Build inside a clean venv, not the Anaconda base env: Anaconda's numpy drags
~500 MB of Intel MKL DLLs in, while the pip wheel ships a far smaller OpenBLAS.

    python -m venv <venv>
    <venv>\\Scripts\\python -m pip install PySide6-Essentials==6.8.3 pillow numpy pyinstaller
    <venv>\\Scripts\\python -m PyInstaller --noconfirm --clean 桌宠助手.spec

This produces the onedir build (fast start). Set PET_ONEFILE=1 for the single
file build instead; it is smaller on disk but unpacks on every start.
"""

import os
from pathlib import Path

PROJECT = Path(SPECPATH)
PROG = PROJECT / '程序'
ONEFILE = os.environ.get('PET_ONEFILE') == '1'

datas = [
    (str(PROG / 'clicker' / 'macro.py'), 'clicker'),
    (str(PROG / 'collector' / 'auto_collect.py'), 'collector'),
    (str(PROG / 'collector' / 'claim_history.py'), 'collector'),
    (str(PROG / 'collector' / 'ui_events.py'), 'collector'),
    (str(PROG / 'collector' / 'monitoring.py'), 'collector'),
    (str(PROG / 'collector' / 'region_capture.py'), 'collector'),
    (str(PROG / 'collector' / 'assets'), 'collector/assets'),
    (str(PROG / 'fonts' / 'ARFangXinShuH7GBK.ttf'), 'fonts'),
    (str(PROG / '背景' / '背景.png'), '背景'),
    (str(PROG / '图标.ico'), '.'),
    (str(PROG / 'update_install.ps1'), '.'),
]

excludes = [
    # 测试与旧 Tk 界面从不参与运行，打包时一并排掉
    'tkinter', 'unittest', 'pytest', '_pytest', 'doctest', 'pydoc', 'pdb',
    'lib2to3',
    'test_jobs', 'test_appearance', 'test_claim_history', 'test_ui_events',
    'test_auto_collect', 'test_monitoring', 'test_region_capture',
    # Pillow 的 AVIF 解码器和 Tk 桥接用不到（AVIF 光一个 .pyd 就 7.5 MB）
    'PIL._avif', 'PIL.ImageTk',
    # 界面只用到 QtCore/QtGui/QtWidgets；PyQt/PySide2 与 PySide6 不能共存
    'PyQt5', 'PyQt6', 'PySide2',
    'PySide6.QtNetwork', 'PySide6.QtQml', 'PySide6.QtQuick',
    'PySide6.QtOpenGL', 'PySide6.QtSvg', 'PySide6.QtTest',
    'PySide6.QtDBus', 'PySide6.QtPrintSupport', 'PySide6.QtSql',
    # 本机装了一堆数据/AI 包，静态分析会把它们拖进来
    'IPython', 'ipykernel', 'ipywidgets', 'jupyter', 'jupyter_client',
    'jupyter_core', 'nbformat', 'nbconvert', 'notebook', 'zmq',
    'matplotlib', 'pandas', 'black', 'yapf',
    # scipy 曾经只用 fftconvolve，现在改为 numpy 自带 FFT；一旦放进来还会
    # 顺着 array_api_compat 把 dask→panel→astropy 整簇拖进包里
    'scipy',
    'dask', 'distributed', 'fsspec', 'panel', 'holoviews', 'hvplot',
    'datashader', 'skimage', 'astropy', 'numba', 'llvmlite', 'cv2',
    'plotly', 'sympy', 'sklearn', 'statsmodels', 'networkx', 'numexpr',
]

a = Analysis(
    [str(PROG / 'launcher.py')],
    pathex=[str(PROG)],
    binaries=[],
    datas=datas,
    # collector 脚本以数据文件方式加载，静态分析看不到它们：显式带上 numpy
    # 与 PIL 的截图/绘图模块（Pillow 的 hook 只收集常用的那几个）。
    hiddenimports=['numpy', 'PIL.ImageGrab', 'PIL.ImageDraw'],
    excludes=excludes,
    noarchive=False,
)

# Qt 只用到 QPainter 的栅格绘制：软件 OpenGL、翻译、多余插件都能删。
# 保留 platforms/qwindows、imageformats 里的 ico/jpeg/webp（窗口图标和
# 背景图片靠它们加载），以及 styles 插件（默认控件外观与源码跑时一致）。
BIN_KEEP = ('\\platforms\\qwindows.dll', '\\imageformats\\qico.dll',
            '\\imageformats\\qjpeg.dll', '\\imageformats\\qwebp.dll')
BIN_DROP = ('\\translations\\', '\\iconengines\\', '\\generic\\', '\\networkinformation\\',
            '\\tls\\', 'opengl32sw.dll', 'qt6network.dll', 'qdirect2d.dll',
            'qminimal.dll', 'qoffscreen.dll', 'qt6svg.dll', 'qt6pdf.dll',
            'qt6qml.dll', 'qt6quick.dll', 'qt6opengl.dll', '\\pil\\_avif')
PLUGIN_DIRS = ('\\platforms\\', '\\imageformats\\')


def keep(entry):
    path = str(entry[0]).replace('/', '\\').lower()
    if any(part in path for part in BIN_DROP):
        return False
    if any(part in path for part in PLUGIN_DIRS):
        return any(path.endswith(part) for part in BIN_KEEP)
    return True


a.binaries = [entry for entry in a.binaries if keep(entry)]
a.datas = [entry for entry in a.datas if keep(entry)]
pyz = PYZ(a.pure)

ICON = str(PROG / '图标.ico')
if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='桌宠助手',
        console=False,
        uac_admin=True,
        icon=ICON,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='桌宠助手',
        console=False,
        uac_admin=True,
        icon=ICON,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        name='桌宠助手',
    )

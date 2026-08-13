# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — Windows build.
# onedir (folder) mode: the user unzips one folder and double-clicks
# "PDF to Excel.exe". No console window. A Tesseract OCR folder placed at
# packaging/tesseract is bundled when present; the app degrades gracefully
# (native PDFs still convert) when it is not.
import os
from PyInstaller.utils.hooks import collect_all

datas = []
tess_dir = os.path.join("packaging", "tesseract")
if os.path.isdir(tess_dir):
    datas.append((tess_dir, "tesseract"))
icon = os.path.join("packaging", "app.ico")
if os.path.exists(icon):
    datas.append((icon, "packaging"))       # the window icon at runtime
else:
    icon = None                             # a missing icon must not
                                            # half-write the app

# CustomTkinter ships themes and fonts as data files; without them the
# window opens unstyled or not at all.
ctk_datas, ctk_binaries, ctk_hidden = collect_all("customtkinter")
datas += ctk_datas

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=ctk_binaries,
    datas=datas,
    hiddenimports=["openpyxl", "pdfplumber", "pypdfium2", "pytesseract",
                   "PIL", "PIL.Image", "gui"] + ctk_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["requests", "urllib3", "http.server"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PDF to Excel",
    debug=False,
    strip=False,
    upx=False,
    console=False,          # never show a terminal window
    icon=icon,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PDF to Excel",
)

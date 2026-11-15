"""Baut ACC-Analyzer(.exe) mit PyInstaller. Wird von build_windows.bat und GitHub Actions benutzt.

--windowed: kein schwarzes Konsolenfenster, nur das Programmfenster.
"""
import os

import PyInstaller.__main__ as pi

pi.run([
    "run_acc_analyzer.py", "--name", "ACC-Analyzer", "--onefile", "--windowed", "--noconfirm", "--clean",
    "--icon", os.path.join("accanalyzer", "assets", "icon.ico"),
    "--add-data", os.pathsep.join([os.path.join("accanalyzer", "assets"), os.path.join("accanalyzer", "assets")]),
    "--collect-submodules", "markdown",
    "--collect-submodules", "accanalyzer",
    "--hidden-import", "matplotlib.backends.backend_agg",
    "--hidden-import", "matplotlib.backends.backend_tkagg",
    "--exclude-module", "PyQt5", "--exclude-module", "PyQt6", "--exclude-module", "PySide6",
    "--exclude-module", "IPython", "--exclude-module", "pytest", "--exclude-module", "scipy",
])

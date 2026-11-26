# Dev helper: double-click launches the unified entry with pythonw (no console).
# End users run WinLinuxTunnel.exe. The GUI starts the agent itself.
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "wlt_main.py"),
               run_name="__main__")

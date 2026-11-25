#!/usr/bin/env python3
"""WinLinuxTunnel unified entry.

  WinLinuxTunnel.exe                 GUI + tray. Starts the agent if needed.
  WinLinuxTunnel.exe --agent         resident agent only (no console, no window)
  WinLinuxTunnel.exe --minimized     GUI starts already in the tray

Dev, from the repo root (system Python, no bundled runtime):

  python win/wlt_main.py
  python win/wlt_main.py --agent
  python win/wlt_main.py --minimized

The agent is one process (HTTP + jobs + tunnel watchdog). The GUI is another
and talks to it only over http://127.0.0.1:8788 with the bearer token.

ASCII comments. Python 3.8+.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

USAGE = """\
WinLinuxTunnel

  WinLinuxTunnel.exe                 open the window (starts the agent if needed)
  WinLinuxTunnel.exe --agent         resident agent only (no window)
  WinLinuxTunnel.exe --minimized     window starts in the notification area

Dev (unpackaged, system Python 3.8+ with Tk):

  python win/wlt_main.py
  python win/wlt_main.py --agent
  python win/wlt_main.py --minimized
"""


def _write_message(text):
    """Print help. A windowed exe has no stdout unless a parent console exists."""
    stream = sys.stdout
    if stream is not None:
        try:
            stream.write(text)
            if not text.endswith("\n"):
                stream.write("\n")
            stream.flush()
            return
        except OSError:
            pass
    if os.name != "nt":
        return
    import ctypes
    kernel32 = ctypes.windll.kernel32
    if kernel32.AttachConsole(0xFFFFFFFF):  # ATTACH_PARENT_PROCESS
        try:
            con = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            con.write("\n" + text)
            if not text.endswith("\n"):
                con.write("\n")
            con.flush()
            return
        except OSError:
            pass
    ctypes.windll.user32.MessageBoxW(0, text, "WinLinuxTunnel", 0x40)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)
    if "--help" in argv or "-h" in argv:
        _write_message(USAGE)
        return 0
    if "--agent" in argv:
        import wlt_agent
        wlt_agent.main()
        return 0
    import wlt_gui
    wlt_gui.main(start_minimized=("--minimized" in argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())

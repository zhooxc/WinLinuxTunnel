' DEV HELPER only. End users double-click WinLinuxTunnel.exe.
' Launches the unified entry with the windowless Python launcher.
' The GUI starts the agent itself when the single-instance lock is free.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.Run "pyw -3 """ & here & "\wlt_main.py""", 0, False

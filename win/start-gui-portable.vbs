' Portable-mode GUI launcher (embeddable runtime under win\runtime).
' End users of the portable zip double-click me. The GUI starts the agent
' itself when the single-instance lock is free. No system Python needed.
' Exe-mode users run WinLinuxTunnel.exe instead; this file is not for them.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = here
sh.Run """" & here & "\runtime\pythonw.exe"" """ & here & "\wlt_main.py""", 0, False
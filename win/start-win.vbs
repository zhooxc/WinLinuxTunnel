' DEV HELPER only. End users run WinLinuxTunnel.exe.
' Hidden launch of the dev crash-restart loop (start-win.cmd).
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.Run """" & here & "\start-win.cmd""", 0, False

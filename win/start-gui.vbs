' wlx GUI - hidden-console launch (Tk window still shows).
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
py = here & "\runtime\pythonw.exe"
If Not fso.FileExists(py) Then
    py = "pythonw"
End If
sh.Run """" & py & """ """ & here & "\wlx_gui.pyw""", 0, False
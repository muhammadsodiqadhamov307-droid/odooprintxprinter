Option Explicit

Dim fso, shell, projectRoot, pythonwPath, pyPath, managerPath, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

If WScript.Arguments.Count > 0 Then
    projectRoot = WScript.Arguments(0)
Else
    projectRoot = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
End If

pythonwPath = projectRoot & "\.venv\Scripts\pythonw.exe"
pyPath = "py.exe"
managerPath = projectRoot & "\agent_manager.py"

If fso.FileExists(pythonwPath) Then
    cmd = """" & pythonwPath & """ """ & managerPath & """"
Else
    cmd = pyPath & " -3.11 """ & managerPath & """"
End If

' 0 = hidden window, False = do not wait
shell.Run cmd, 0, False

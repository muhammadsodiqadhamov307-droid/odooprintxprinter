Option Explicit

Dim fso, shell, projectRoot, pythonwPath, pythonPath, pyPath, managerPath, installScript, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

If WScript.Arguments.Count > 0 Then
    projectRoot = WScript.Arguments(0)
Else
    projectRoot = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
End If

pythonwPath = projectRoot & "\.venv\Scripts\pythonw.exe"
pythonPath = projectRoot & "\.venv\Scripts\python.exe"
pyPath = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Microsoft\WindowsApps\py.exe"
managerPath = projectRoot & "\agent_manager.py"
installScript = projectRoot & "\deploy\windows\install_service.ps1"

If Not fso.FileExists(managerPath) Then
    MsgBox "Manager file not found:" & vbCrLf & managerPath, vbCritical, "Odoo Print Agent Manager"
    WScript.Quit 1
End If

If fso.FileExists(pythonwPath) Then
    cmd = """" & pythonwPath & """ """ & managerPath & """"
    shell.Run cmd, 0, False
    WScript.Quit 0
End If

If fso.FileExists(pythonPath) Then
    cmd = """" & pythonPath & """ """ & managerPath & """"
    shell.Run cmd, 0, False
    WScript.Quit 0
End If

If fso.FileExists(pyPath) Then
    cmd = pyPath & " -3.11 """ & managerPath & """"
    shell.Run cmd, 0, False
    WScript.Quit 0
End If

If fso.FileExists(installScript) Then
    If MsgBox("Python runtime is missing for Odoo Print Agent." & vbCrLf & vbCrLf & _
              "Run automatic repair now? (requires Administrator)", _
              vbYesNo + vbQuestion, "Odoo Print Agent Manager") = vbYes Then
        CreateObject("Shell.Application").ShellExecute _
            "powershell.exe", _
            "-ExecutionPolicy Bypass -File """ & installScript & _
            """ -ServiceName OdooPrintAgent -ProjectRoot """ & projectRoot & _
            """ -AutoInstallPython", _
            "", _
            "runas", _
            1
    End If
End If

MsgBox "Could not find Python runtime to launch Odoo Print Agent Manager." & vbCrLf & _
       "Project root: " & projectRoot, vbCritical, "Odoo Print Agent Manager"
WScript.Quit 2

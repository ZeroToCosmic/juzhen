Option Explicit

Dim shell, filesystem, projectRoot, pythonwPath, launcherPath, command
Set shell = CreateObject("WScript.Shell")
Set filesystem = CreateObject("Scripting.FileSystemObject")

projectRoot = filesystem.GetParentFolderName(WScript.ScriptFullName)
pythonwPath = filesystem.BuildPath(projectRoot, ".venv\Scripts\pythonw.exe")
launcherPath = filesystem.BuildPath(projectRoot, "launcher.py")

If Not filesystem.FileExists(pythonwPath) Then
    MsgBox "Launcher environment is incomplete. Reinstall dependencies.", 16, "Launcher startup failed"
    WScript.Quit 1
End If

If Not filesystem.FileExists(launcherPath) Then
    MsgBox "Unable to find launcher.py. Reinstall the application.", 16, "Launcher startup failed"
    WScript.Quit 1
End If

shell.CurrentDirectory = projectRoot
command = """" & pythonwPath & """ """ & launcherPath & """"
On Error Resume Next
shell.Run command, 0, False
If Err.Number <> 0 Then
    Err.Clear
    On Error GoTo 0
    MsgBox "Unable to start launcher. Reinstall dependencies and try again.", 16, "Launcher startup failed"
    WScript.Quit 1
End If
On Error GoTo 0

' md2pdf - Launcher silencioso (sem janela do CMD)
Set WshShell = CreateObject("WScript.Shell")
strPath = Replace(WScript.ScriptFullName, WScript.ScriptName, "")
WshShell.CurrentDirectory = strPath
WshShell.Run "cmd /c """ & strPath & "start.bat""", 0, False

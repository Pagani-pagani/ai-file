' AI File 啟動器 v1（備援）
' 不會跳黑視窗，用 pythonw.exe 啟動本地端程式。
Dim WshShell, scriptPath
Set WshShell = CreateObject("WScript.Shell")
scriptPath = Replace(WScript.ScriptFullName, WScript.ScriptName, "") & "ai_file_agent.py"
WshShell.Run "pythonw.exe """ & scriptPath & """", 0, False
Set WshShell = Nothing

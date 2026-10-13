' Starts the Gemini proxy in the background with no console window.
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run "cmd /c """"" & dir & "\Gemini Wrapper.bat""""", 0, False

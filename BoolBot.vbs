' Double-click to start BoolBot in the background (no console window).
' Updates yt-dlp first (YouTube breaks old versions), then starts the bot.
' Logs: boolbot.log in this folder. Stop it with StopBoolBot.vbs.
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
sh.Run "python -m pip install -U --quiet ""yt-dlp[default]""", 0, True
sh.Run "pythonw """ & dir & "\bot.py""", 0, False

' Double-click to stop BoolBot.
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT * FROM Win32_Process WHERE Name LIKE 'python%' AND CommandLine LIKE '%boolbot%bot.py%'")
n = 0
For Each p In procs
  p.Terminate()
  n = n + 1
Next
If n = 0 Then
  MsgBox "BoolBot wasn't running.", 64, "BoolBot"
Else
  MsgBox "BoolBot stopped.", 64, "BoolBot"
End If

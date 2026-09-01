Option Explicit

Dim arguments, executablePath, workingDirectory, commandLine, index
Dim shell, exitCode

Set arguments = WScript.Arguments
If arguments.Count < 3 Then
    WScript.Quit 64
End If

executablePath = arguments(0)
workingDirectory = arguments(1)
commandLine = QuoteArgument(executablePath)

For index = 2 To arguments.Count - 1
    commandLine = commandLine & " " & QuoteArgument(arguments(index))
Next

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = workingDirectory

' windowStyle=0 hides the console chain; waitOnReturn=True propagates the
' Python worker exit code to Windows Task Scheduler.
exitCode = shell.Run(commandLine, 0, True)
WScript.Quit exitCode

Function QuoteArgument(ByVal value)
    If InStr(value, Chr(34)) > 0 Then
        WScript.Quit 65
    End If
    QuoteArgument = Chr(34) & value & Chr(34)
End Function

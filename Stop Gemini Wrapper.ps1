# Graceful stop: Ctrl+C the hidden proxy console (uvicorn shuts down and closes Chrome cleanly),
# then force-kill only if it is still alive after 20s.
$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $conn) { Write-Host 'Gemini proxy nao esta rodando.'; return }
$procId = $conn.OwningProcess
# the listener may be the venv child; Ctrl+C must go to the process owning the console
$target = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId").ParentProcessId
Add-Type @"
using System; using System.Runtime.InteropServices;
public class Ctl {
 [DllImport("kernel32.dll")] public static extern bool FreeConsole();
 [DllImport("kernel32.dll")] public static extern bool AttachConsole(uint pid);
 [DllImport("kernel32.dll")] public static extern bool SetConsoleCtrlHandler(IntPtr h, bool add);
 [DllImport("kernel32.dll")] public static extern bool GenerateConsoleCtrlEvent(uint ev, uint grp);
}
"@
[Ctl]::FreeConsole() | Out-Null
foreach ($id in @($procId, $target)) {
  if ([Ctl]::AttachConsole([uint32]$id)) {
    [Ctl]::SetConsoleCtrlHandler([IntPtr]::Zero, $true) | Out-Null
    [Ctl]::GenerateConsoleCtrlEvent(0, 0) | Out-Null
    [Ctl]::FreeConsole() | Out-Null
    break
  }
}
for ($i = 0; $i -lt 20 -and (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue); $i++) { Start-Sleep 1 }
if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
  Write-Host 'Encerramento gracioso nao funcionou; forcando...'
  taskkill /PID $procId /F /T | Out-Null
} else { Write-Host "Gemini proxy encerrado com seguranca (${i}s)." }

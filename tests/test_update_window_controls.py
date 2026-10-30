"""Native helper controls exercised without starting an installer or touching an app."""

import os
import subprocess

import pytest

from s2s import updater


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Forms updater window")


@pytest.mark.parametrize("dark", [True, False])
def test_minimize_and_confirmed_close_keep_helper_and_launch_guard_alive(tmp_path, dark):
    appearance = dict(window="#1d2228" if dark else "#f5f6f8",
                      text="#e1e6ec" if dark else "#20252b", border="#3b4652",
                      panel="#181d23", primary="#416f94", muted="#96a9bc",
                      font="Segoe UI", font_size=9)
    script, _ = updater.installer_script(tmp_path / "app", tmp_path / "unused-setup.exe", 2147483647,
                                         appearance=appearance)
    text = script.read_text(encoding="utf-8-sig")
    # Run the actual UI and event handler, stopping before parent/installer handling.
    main = text.index("try {\n    Add-Type -AssemblyName System.Windows.Forms")
    show = text.index("    $form.Show()", main)
    harness = text[:main] + text[main + len("try {\n"):show]
    harness += r'''
function Assert($condition, $message) { if (-not $condition) { throw $message } }
$form.StartPosition = 'Manual'
$form.Location = New-Object Drawing.Point(-30000, -30000)
$form.ShowInTaskbar = $false
$form.Show()
[Windows.Forms.Application]::DoEvents()
Assert ($form.ControlBox -and $form.MinimizeBox -and -not $form.MaximizeBox) 'Missing native window controls'
$bitmap = New-Object Drawing.Bitmap($form.Width, $form.Height)
$form.DrawToBitmap($bitmap, (New-Object Drawing.Rectangle(0, 0, $form.Width, $form.Height)))
$bitmap.Save((Join-Path $PSScriptRoot 'progress.png'))
$bitmap.Dispose()
Set-Content -LiteralPath $active -Value 'guard retained'
$form.WindowState = [Windows.Forms.FormWindowState]::Minimized
[Windows.Forms.Application]::DoEvents()
Assert ($form.WindowState -eq [Windows.Forms.FormWindowState]::Minimized) 'Minimize failed'
Assert (-not $form.IsDisposed) 'Minimize disposed the updater'
$form.WindowState = [Windows.Forms.FormWindowState]::Normal
$script:choice = 'Keep open'
$script:questions = 0
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 50
$timer.Add_Tick({
    foreach ($question in @([Windows.Forms.Application]::OpenForms)) {
        if ($question.Text -ne 'Close update window?') { continue }
        Assert ($question.BackColor -eq $form.BackColor) 'Confirmation theme changed'
        Assert ($question.AcceptButton.Text -eq 'Keep open') 'Enter must keep the window open'
        Assert ($question.CancelButton.Text -eq 'Keep open') 'Escape must keep the window open'
        Assert (($question.Controls | Where-Object { $_.Text -like '*continue in the background*' }).Count -eq 1) 'Background behavior unexplained'
        $script:questions++
        $bitmap = New-Object Drawing.Bitmap($question.Width, $question.Height)
        $question.DrawToBitmap($bitmap, (New-Object Drawing.Rectangle(0, 0, $question.Width, $question.Height)))
        $bitmap.Save((Join-Path $PSScriptRoot ('confirmation-' + $script:questions + '.png')))
        $bitmap.Dispose()
        ($question.Controls | Where-Object { $_.Text -eq $script:choice }).PerformClick()
    }
})
try {
    $timer.Start()
    $form.Close()
    Assert ($script:questions -eq 1 -and $form.Visible) 'Declining close must retain progress'
    $script:choice = 'Close window'
    $form.Close()
    Assert ($script:questions -eq 2 -and -not $form.Visible) 'Accepting close must hide progress'
    Assert (-not $form.IsDisposed) 'Close must not destroy controls used by installation'
    Assert (Test-Path -LiteralPath $active) 'Close removed the installation guard'
    Assert (-not $allowClose) 'Close changed the helper exit state'
    Assert ($null -eq $job) 'UI test unexpectedly started an installer'
    Write-Output 'PASS: minimize, close confirmation, safe background continuation'
} finally {
    $timer.Stop()
    $timer.Dispose()
    $form.Dispose()
    $animation.Dispose()
    Remove-Item -LiteralPath $active -Force -ErrorAction SilentlyContinue
}
'''
    harness_path = tmp_path / "window-controls.ps1"
    harness_path.write_text(harness, encoding="utf-8-sig")
    result = subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                             "-File", str(harness_path)], capture_output=True, timeout=35)
    assert result.returncode == 0, (result.stdout + result.stderr).decode(errors="replace")
    assert b"PASS: minimize" in result.stdout


def test_hidden_helper_finishes_its_worker_and_reopens_recovery_on_failure(tmp_path):
    script, _ = updater.installer_script(tmp_path / "never-launched-app", tmp_path / "never-run-setup.exe",
                                         2147483647)
    text = script.read_text(encoding="utf-8-sig")
    start = text.index("    $job = [PowerShell]::Create()")
    end = text.index("    $pending = $job.BeginInvoke()", start)
    # No installer is started. A real worker completes with failure after the progress window is hidden.
    text = text[:start] + """    $job = [PowerShell]::Create()
    [void]$job.AddScript({ Start-Sleep -Milliseconds 800; 1 })
""" + text[end:]
    driver = r'''
    $form.StartPosition = 'Manual'
    $form.Location = New-Object Drawing.Point(-30000, -30000)
    $form.ShowInTaskbar = $false
    $script:closeRequested = $false
    $script:backgroundSafe = $false
    $script:recovered = $false
    $answerTimer = New-Object Windows.Forms.Timer
    $answerTimer.Interval = 30
    $answerTimer.Add_Tick({
        foreach ($question in @([Windows.Forms.Application]::OpenForms)) {
            if ($question.Text -eq 'Close update window?') {
                ($question.Controls | Where-Object { $_.Text -eq 'Close window' }).PerformClick()
            }
        }
    })
    $driverTimer = New-Object Windows.Forms.Timer
    $driverTimer.Interval = 50
    $driverTimer.Add_Tick({
        if (-not $script:closeRequested -and $null -ne $job) {
            $script:closeRequested = $true
            $form.Close()
            $script:backgroundSafe = (-not $form.Visible -and -not $form.IsDisposed -and (Test-Path -LiteralPath $active))
        }
        if ($title.Text -eq 'Update needs attention') {
            $script:recovered = ($form.Visible -and $form.WindowState -eq [Windows.Forms.FormWindowState]::Normal)
            $form.Close()
        }
    })
    $answerTimer.Start()
    $driverTimer.Start()
'''
    text = text.replace("    $form.Show()", driver + "\n    $form.Show()", 1)
    text = text.replace("if (-not $success) { exit 1 }", r'''
$answerTimer.Dispose()
$driverTimer.Dispose()
if (-not $script:backgroundSafe -or -not $script:recovered) { throw 'Hidden failure recovery failed' }
if (Test-Path -LiteralPath $active) { throw 'Finished failed update retained its launch guard' }
Write-Output 'PASS: hidden worker completed and recovery reappeared'
if (-not $success) { exit 1 }
''')
    harness = tmp_path / "failure-recovery.ps1"
    harness.write_text(text, encoding="utf-8-sig")
    result = subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                             "-File", str(harness)], capture_output=True, timeout=35)
    assert result.returncode == 1
    assert b"PASS: hidden worker completed" in result.stdout, (result.stdout + result.stderr).decode(errors="replace")
    assert "installed 1" in updater.result_file().read_text(encoding="utf-8-sig")

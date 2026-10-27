# Runs outside the installation directory so replacing Qt cannot destroy this window.
$ErrorActionPreference = 'Stop'
$app = @@APP@@
$setup = @@SETUP@@
$parentPid = @@PID@@
$result = @@RESULT@@
$active = @@ACTIVE@@
$appearance = ConvertFrom-Json @@STYLE@@
$ready = Join-Path $PSScriptRoot 'ready'
$log = Join-Path (Split-Path $result) 'update-install.log'
$trace = Join-Path (Split-Path $result) 'update-helper.log'
$success = $false
$job = $null
$form = $null
$animation = $null
$allowClose = $false
function Trace($message) {
    Add-Content -LiteralPath $trace -Value ((Get-Date -Format o) + ' ' + $message)
}
try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'Updating S2S'
    $form.ClientSize = New-Object System.Drawing.Size(480, 205)
    $form.AutoScaleMode = 'Dpi'
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $true
    $form.ControlBox = $false
    $form.Add_FormClosing({ param($sender, $event) if (-not $allowClose) { $event.Cancel = $true } })
    $iconPath = Join-Path $PSScriptRoot 'icon.png'
    if (Test-Path -LiteralPath $iconPath) {
        $iconBitmap = [Drawing.Bitmap]::FromFile($iconPath)
        $form.Icon = [Drawing.Icon]::FromHandle($iconBitmap.GetHicon())
    }
    if ($appearance.window) {
        $form.BackColor = [Drawing.ColorTranslator]::FromHtml($appearance.window)
        $form.ForeColor = [Drawing.ColorTranslator]::FromHtml($appearance.text)
        $form.Font = New-Object Drawing.Font($appearance.font, [single]$appearance.font_size)
    }
    $title = New-Object System.Windows.Forms.Label
    $title.SetBounds(20, 20, 440, 30)
    $title.Text = 'Updating S2S'
    $title.Font = New-Object Drawing.Font($form.Font.FontFamily, 13, ([Drawing.FontStyle]::Bold))
    $label = New-Object System.Windows.Forms.Label
    $label.SetBounds(20, 60, 440, 38)
    $label.Text = 'Closing S2S before installing the update...'
    $bar = New-Object System.Windows.Forms.Panel
    $bar.SetBounds(20, 108, 440, 10)
    $bar.BackColor = [Drawing.SystemColors]::ControlLight
    $fill = New-Object System.Windows.Forms.Panel
    $fill.SetBounds(-100, 0, 100, 10)
    $fill.BackColor = [Drawing.Color]::SteelBlue
    if ($appearance.panel) {
        $bar.BackColor = [Drawing.ColorTranslator]::FromHtml($appearance.panel)
        $fill.BackColor = [Drawing.ColorTranslator]::FromHtml($appearance.primary)
    }
    $bar.Controls.Add($fill)
    $animation = New-Object System.Windows.Forms.Timer
    $animation.Interval = 20
    $animation.Add_Tick({
        $fill.Left = $fill.Left + 5
        if ($fill.Left -gt $bar.Width) { $fill.Left = -$fill.Width }
    })
    $animation.Start()
    $detail = New-Object System.Windows.Forms.Label
    $detail.SetBounds(20, 134, 440, 50)
    $detail.Text = 'S2S will reopen automatically when the update is ready.'
    if ($appearance.muted) { $detail.ForeColor = [Drawing.ColorTranslator]::FromHtml($appearance.muted) }
    $form.Controls.AddRange(@($title, $label, $bar, $detail))
    # Match the app's dark/light title bar on supported Windows versions.
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class S2SUpdateFrame {
    [DllImport("dwmapi.dll")]
    public static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);
}
'@
    $form.Show()
    $dark = [int]($form.BackColor.GetBrightness() -lt 0.5)
    [void][S2SUpdateFrame]::DwmSetWindowAttribute($form.Handle, 20, [ref]$dark, 4)
    [System.Windows.Forms.Application]::DoEvents()
    @{pid=$PID; target=$app; started=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); phase='installing'} |
        ConvertTo-Json -Compress | Set-Content -LiteralPath $active -Encoding UTF8
    Set-Content -LiteralPath $ready -Value 'ready'
    Trace 'Progress window ready; waiting for S2S to exit'
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while (Get-Process -Id $parentPid -ErrorAction SilentlyContinue) {
        if ($clock.Elapsed.TotalSeconds -gt 120) { throw 'S2S did not close. The installer was not started.' }
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 100
    }
    $label.Text = 'Installing the update...'
    Trace 'Starting installer'
    # A separate runspace keeps the window responsive while Windows scans/starts setup.
    $job = [PowerShell]::Create()
    [void]$job.AddScript({
        param($setupPath, $appPath, $logPath)
        $ErrorActionPreference = 'Stop'
        $p = Start-Process -FilePath $setupPath -Wait -PassThru -ArgumentList @(
            '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
            ('/DIR="' + $appPath + '"'), ('/LOG="' + $logPath + '"'))
        $p.ExitCode
    }).AddArgument($setup).AddArgument($app).AddArgument($log)
    $pending = $job.BeginInvoke()
    while (-not $pending.IsCompleted) {
        $detail.Text = ('Elapsed: {0:mm\:ss}. S2S will reopen automatically.' -f $clock.Elapsed)
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 100
    }
    $codes = @($job.EndInvoke($pending))
    if ($job.HadErrors -or $codes.Count -ne 1) { throw 'Windows could not run the installer.' }
    $code = [int]$codes[0]
    Trace ('Installer exited: ' + $code)
    Add-Content -LiteralPath $result -Value ('installed ' + $code)
    if ($code -ne 0) { throw ('The installer could not finish (error code ' + $code + '). View the log for details, or download the installer to retry.') }
    $label.Text = 'Update installed. Opening S2S...'
    [System.Windows.Forms.Application]::DoEvents()
    # Files are complete now; allow the new app through its startup guard.
    @{pid=$PID; target=$app; started=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); phase='complete'} |
        ConvertTo-Json -Compress | Set-Content -LiteralPath $active -Encoding UTF8
    $restarted = Start-Process -FilePath (Join-Path $app 'S2S.exe') -WorkingDirectory $app -PassThru
    Trace 'Restart requested'
    $opening = [Diagnostics.Stopwatch]::StartNew()
    while (-not $restarted.HasExited -and $opening.Elapsed.TotalSeconds -lt 30) {
        $restarted.Refresh()
        if ($restarted.MainWindowHandle -ne [IntPtr]::Zero) { break }
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 100
    }
    $success = $true
} catch {
    $message = $_.Exception.Message
    Trace ('Failed: ' + $message)
    # Keep errors inside this same themed window, with recovery actions.
    Remove-Item -LiteralPath $active -Force -ErrorAction SilentlyContinue
    if ($form) {
        $allowClose = $true
        $animation.Stop()
        $bar.Visible = $false
        $form.ClientSize = New-Object System.Drawing.Size(480, 300)
        $form.ControlBox = $true
        $title.Text = 'Update needs attention'
        $label.SetBounds(20, 64, 440, 110)
        $label.Text = $message
        $detail.SetBounds(20, 180, 440, 38)
        $detail.Text = 'S2S was not restarted. Download the installer to retry.'
        foreach ($spec in @(@('View log', $log, 20), @('Close', 'close', 170),
                @('Download', 'https://github.com/xanman04/stream2soulseek/releases/latest', 320))) {
            $button = New-Object System.Windows.Forms.Button
            $button.SetBounds([int]$spec[2], 240, 130, 34)
            $button.Text = $spec[0]
            $button.Tag = $spec[1]
            $button.FlatStyle = 'Flat'
            $button.BackColor = $bar.BackColor
            if ($appearance.border) { $button.FlatAppearance.BorderColor = [Drawing.ColorTranslator]::FromHtml($appearance.border) }
            if ($button.Text -eq 'Download' -and $appearance.primary) {
                $button.BackColor = [Drawing.ColorTranslator]::FromHtml($appearance.primary)
                $button.ForeColor = [Drawing.Color]::White
            }
            $button.Add_Click({
                param($sender, $event)
                if ($sender.Tag -eq 'close') { $form.Close() }
                else { Start-Process -FilePath ([string]$sender.Tag) }
            })
            $form.Controls.Add($button)
        }
        while ($form.Visible) {
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 100
        }
    }
} finally {
    if ($job) { $job.Dispose() }
    Remove-Item -LiteralPath $active -Force -ErrorAction SilentlyContinue
    if ($form) { $form.Dispose() }
    if ($iconBitmap) { $iconBitmap.Dispose() }
    if ($animation) { $animation.Dispose() }
    if ($success) {
        Remove-Item -LiteralPath $setup -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $PSScriptRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
if (-not $success) { exit 1 }

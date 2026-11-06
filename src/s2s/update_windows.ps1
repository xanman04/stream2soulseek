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
$frameClock = [Diagnostics.Stopwatch]::StartNew()
$allowClose = $false
function Trace($message) {
    Add-Content -LiteralPath $trace -Value ((Get-Date -Format o) + ' ' + $message)
}
function Wait-UpdatePoll {
    # Poll the installer at the old rate without starving the window's paint and input messages.
    $poll = [Diagnostics.Stopwatch]::StartNew()
    do {
        [System.Windows.Forms.Application]::DoEvents()
        if ($frameClock.ElapsedMilliseconds -ge 32 -and $form.Visible -and $form.WindowState -ne 'Minimized') {
            # Paint now: queued timer/paint messages can be delayed while setup starts.
            $bar.Advance()
            $frameClock.Restart()
        }
        Start-Sleep -Milliseconds 12
    } while ($poll.ElapsedMilliseconds -lt 100)
}
function Confirm-CloseUpdateWindow($owner) {
    $question = New-Object System.Windows.Forms.Form
    try {
        $question.Text = 'Close update window?'
        $question.ClientSize = New-Object Drawing.Size(480, 180)
        $question.StartPosition = 'CenterParent'
        $question.FormBorderStyle = 'FixedDialog'
        $question.MaximizeBox = $false
        $question.MinimizeBox = $false
        $question.ShowInTaskbar = $false
        $question.BackColor = $owner.BackColor
        $question.ForeColor = $owner.ForeColor
        $question.Font = $owner.Font
        $message = New-Object System.Windows.Forms.Label
        $message.SetBounds(20, 20, 440, 94)
        $message.Text = "Are you sure you want to close this update window?`r`n`r`nThe update will continue in the background. S2S will reopen automatically when it is finished."
        $keep = New-Object System.Windows.Forms.Button
        $keep.SetBounds(180, 126, 130, 34)
        $keep.Text = 'Keep open'
        $keep.DialogResult = [Windows.Forms.DialogResult]::No
        $close = New-Object System.Windows.Forms.Button
        $close.SetBounds(330, 126, 130, 34)
        $close.Text = 'Close window'
        $close.DialogResult = [Windows.Forms.DialogResult]::Yes
        foreach ($button in @($keep, $close)) {
            $button.FlatStyle = 'Flat'
            $button.BackColor = $owner.BackColor
            $button.ForeColor = $owner.ForeColor
            if ($appearance.border) { $button.FlatAppearance.BorderColor = [Drawing.ColorTranslator]::FromHtml($appearance.border) }
        }
        $question.Controls.AddRange(@($message, $keep, $close))
        $question.AcceptButton = $keep
        $question.CancelButton = $keep
        $question.Add_Shown({
            $darkQuestion = [int]($question.BackColor.GetBrightness() -lt 0.5)
            [void][S2SUpdateFrame]::DwmSetWindowAttribute($question.Handle, 20, [ref]$darkQuestion, 4)
        })
        return $question.ShowDialog($owner) -eq [Windows.Forms.DialogResult]::Yes
    } finally {
        $question.Dispose()
    }
}
try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition @'
using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;

public sealed class S2SUpdateProgress : Panel {
    private readonly Stopwatch clock = Stopwatch.StartNew();
    public Color Accent { get; set; }
    public float Position { get; private set; }
    public S2SUpdateProgress() {
        DoubleBuffered = true;
        ResizeRedraw = true;
    }

    public void Advance() {
        double phase = clock.Elapsed.TotalSeconds * Math.PI / 1.6;
        Position = (float)((1 - Math.Cos(phase)) / 2);
        Refresh();
    }

    protected override void OnPaint(PaintEventArgs e) {
        if (Width <= 0 || Height <= 0) return;
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        float radius = Math.Min(Width, Height) / 2f;
        float span = Math.Max(0, Width * 0.25f - 2 * radius);
        float travel = Math.Max(0, Width - span - 2 * radius);
        float left = travel * Position;
        using (var brush = new SolidBrush(Accent)) {
            e.Graphics.FillEllipse(brush, left, 0, radius * 2, Height);
            e.Graphics.FillRectangle(brush, left + radius, 0, span, Height);
            e.Graphics.FillEllipse(brush, left + span, 0, radius * 2, Height);
        }
    }
}

public static class S2SUpdateFrame {
    [System.Runtime.InteropServices.DllImport("dwmapi.dll")]
    public static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);
}
'@
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'Updating S2S'
    $form.ClientSize = New-Object System.Drawing.Size(480, 196)
    $form.AutoScaleMode = 'Dpi'
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $true
    $form.ControlBox = $true
    $form.Add_FormClosing({
        param($sender, $event)
        if (-not $allowClose) {
            # Never terminate setup or clear the launch guard while files are being replaced.
            $event.Cancel = $true
            if (Confirm-CloseUpdateWindow $sender) {
                Trace 'Progress window closed; installation continues in the background'
                $sender.Hide()
            }
        }
    })
    $iconPath = Join-Path $PSScriptRoot 'icon.png'
    $headingLeft = 24
    if (Test-Path -LiteralPath $iconPath) {
        $iconBitmap = [Drawing.Bitmap]::FromFile($iconPath)
        $form.Icon = [Drawing.Icon]::FromHandle($iconBitmap.GetHicon())
        $logo = New-Object System.Windows.Forms.PictureBox
        $logo.SetBounds(24, 25, 38, 38)
        $logo.SizeMode = 'Zoom'
        $logo.Image = $iconBitmap
        $form.Controls.Add($logo)
        $headingLeft = 76
    }
    if ($appearance.window) {
        $form.BackColor = [Drawing.ColorTranslator]::FromHtml($appearance.window)
        $form.ForeColor = [Drawing.ColorTranslator]::FromHtml($appearance.text)
        $form.Font = New-Object Drawing.Font($appearance.font, [single]$appearance.font_size)
    }
    $title = New-Object System.Windows.Forms.Label
    $title.SetBounds($headingLeft, 20, (456 - $headingLeft), 30)
    $title.Text = 'Updating S2S'
    $title.Font = New-Object Drawing.Font($form.Font.FontFamily, 16, ([Drawing.FontStyle]::Bold))
    $label = New-Object System.Windows.Forms.Label
    $label.SetBounds($headingLeft, 53, (456 - $headingLeft), 25)
    $label.Text = 'Waiting for S2S to close…'
    $bar = New-Object S2SUpdateProgress
    $bar.SetBounds(24, 101, 432, 8)
    $bar.AccessibleName = 'Update in progress'
    $bar.BackColor = [Drawing.SystemColors]::ControlLight
    $bar.Accent = [Drawing.Color]::SteelBlue
    if ($appearance.panel) {
        $bar.BackColor = [Drawing.ColorTranslator]::FromHtml($appearance.panel)
        $bar.Accent = [Drawing.ColorTranslator]::FromHtml($appearance.primary)
    }
    $detail = New-Object System.Windows.Forms.Label
    $detail.SetBounds(24, 126, 432, 25)
    $detail.Text = 'S2S will reopen automatically.'
    $elapsed = New-Object System.Windows.Forms.Label
    $elapsed.SetBounds(24, 154, 432, 24)
    if ($appearance.muted) { $detail.ForeColor = [Drawing.ColorTranslator]::FromHtml($appearance.muted) }
    if ($appearance.muted) { $elapsed.ForeColor = $detail.ForeColor }
    $form.Controls.AddRange(@($title, $label, $bar, $detail, $elapsed))
    # Match the app's dark/light title bar on supported Windows versions.
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
        Wait-UpdatePoll
    }
    $label.Text = 'Installing the update…'
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
        $elapsedText = 'Elapsed: {0:mm\:ss}' -f $clock.Elapsed
        if ($elapsed.Text -ne $elapsedText) { $elapsed.Text = $elapsedText }
        Wait-UpdatePoll
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
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = Join-Path $app 'S2S.exe'
    $startInfo.WorkingDirectory = $app
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    Trace 'Starting S2S directly'
    $restarted = [Diagnostics.Process]::Start($startInfo)
    Trace 'Restart requested'
    $opening = [Diagnostics.Stopwatch]::StartNew()
    while (-not $restarted.HasExited -and $opening.Elapsed.TotalSeconds -lt 30) {
        $restarted.Refresh()
        if ($restarted.MainWindowHandle -ne [IntPtr]::Zero) { break }
        Wait-UpdatePoll
    }
    $success = $true
} catch {
    $message = $_.Exception.Message
    Trace ('Failed: ' + $message)
    # Keep errors inside this same themed window, with recovery actions.
    Remove-Item -LiteralPath $active -Force -ErrorAction SilentlyContinue
    if ($form) {
        $allowClose = $true
        if ($bar) { $bar.Visible = $false }
        if ($elapsed) { $elapsed.Visible = $false }
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
        # A background update must still make failures and recovery actions visible.
        $form.WindowState = [Windows.Forms.FormWindowState]::Normal
        $form.Show()
        $form.Activate()
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
    if ($success) {
        Remove-Item -LiteralPath $setup -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $PSScriptRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
if (-not $success) { exit 1 }

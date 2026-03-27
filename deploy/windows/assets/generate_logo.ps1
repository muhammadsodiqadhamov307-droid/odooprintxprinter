Add-Type -AssemblyName System.Drawing

$ErrorActionPreference = "Stop"

$assetDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pngPath = Join-Path $assetDir "app_logo.png"
$icoPath = Join-Path $assetDir "app_logo.ico"

$size = 256
$bmp = New-Object System.Drawing.Bitmap($size, $size)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$g.Clear([System.Drawing.Color]::FromArgb(24, 36, 53))

$brushA = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 83, 132, 238))
$brushB = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 46, 196, 182))
$brushText = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)

$g.FillEllipse($brushA, 20, 20, 216, 216)
$g.FillEllipse($brushB, 50, 50, 156, 156)

$font = New-Object System.Drawing.Font("Segoe UI", 54, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
$format = New-Object System.Drawing.StringFormat
$format.Alignment = [System.Drawing.StringAlignment]::Center
$format.LineAlignment = [System.Drawing.StringAlignment]::Center
$rect = New-Object System.Drawing.RectangleF(0, 0, $size, $size)
$g.DrawString("OP", $font, $brushText, $rect, $format)

$bmp.Save($pngPath, [System.Drawing.Imaging.ImageFormat]::Png)

$icon = [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
$fs = New-Object System.IO.FileStream($icoPath, [System.IO.FileMode]::Create)
$icon.Save($fs)
$fs.Close()

$font.Dispose()
$brushA.Dispose()
$brushB.Dispose()
$brushText.Dispose()
$g.Dispose()
$bmp.Dispose()
$icon.Dispose()

Write-Host "Generated:"
Write-Host " - $pngPath"
Write-Host " - $icoPath"

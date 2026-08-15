# SUPERSEDED as the build path by packaging/derive-icons.mjs, which now runs
# on every `npm run dist*` (see desktop/package.json) and derives icon.ico
# directly from web/public/nh-mark-512.png with a dependency-free Node
# pipeline — no committed icon.icns or icon.ico any more, on either platform.
# This script is kept as a Windows-native fallback for a box with no Node, and
# its logic is UNCHANGED: it still reads desktop\build\icon.icns (now itself
# freshly derived, not committed) rather than the PNG master directly.
#
# Generate desktop\build\icon.ico from the SAME artwork the macOS build uses.
#
# WHY THIS EXISTS. The Windows icon must be the same mark as the Mac one, and
# there must be exactly one place that artwork comes from. The obvious source —
# the site's mark-dark PNGs that electron-builder.config.cjs credits for
# icon.icns — is NOT in this repo (verified: no file matching `mark-dark*`
# exists anywhere in the tree). The only copy of the brand mark that ships with
# this repository is desktop\build\icon.icns, so the .ico is derived from it.
# That keeps a single source of truth rather than introducing a second, and it
# means the two platforms cannot drift to different artwork.
#
# Output entries are PNG-compressed, which every supported Windows understands
# (Vista+). 256x256 is required by electron-builder's NSIS target.

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$Root  = Split-Path -Parent $PSScriptRoot
$Icns  = Join-Path $Root 'desktop\build\icon.icns'
$IcoOut = Join-Path $Root 'desktop\build\icon.ico'

if (-not (Test-Path $Icns)) { Write-Host "FAIL: no $Icns" -ForegroundColor Red; exit 1 }

# --- pull the largest embedded PNG out of the .icns ---------------------- #
# .icns layout: 'icns' + total length (big-endian), then a flat sequence of
# chunks, each: 4-byte OSType + 4-byte big-endian length (INCLUDING the 8-byte
# header) + payload. Modern size variants (ic07..ic10) carry a whole PNG file as
# the payload; older ones carry raw/RLE bitmaps, which are skipped by testing
# for the PNG magic rather than by trusting the OSType.
$bytes = [System.IO.File]::ReadAllBytes($Icns)
if ([System.Text.Encoding]::ASCII.GetString($bytes, 0, 4) -ne 'icns') {
  Write-Host 'FAIL: not an icns file' -ForegroundColor Red; exit 1
}

function Read-BE32([byte[]]$b, [int]$off) {
  return ([int]$b[$off] -shl 24) -bor ([int]$b[$off+1] -shl 16) -bor
         ([int]$b[$off+2] -shl 8) -bor [int]$b[$off+3]
}

$pngMagic = [byte[]](0x89,0x50,0x4E,0x47,0x0D,0x0A,0x1A,0x0A)
$best = $null; $bestPx = 0
$pos = 8
$total = Read-BE32 $bytes 4
while ($pos + 8 -le [Math]::Min($total, $bytes.Length)) {
  $type = [System.Text.Encoding]::ASCII.GetString($bytes, $pos, 4)
  $len  = Read-BE32 $bytes ($pos + 4)
  if ($len -lt 8 -or ($pos + $len) -gt $bytes.Length) { break }
  $dataOff = $pos + 8
  $dataLen = $len - 8
  if ($dataLen -gt 8) {
    $isPng = $true
    for ($i = 0; $i -lt 8; $i++) {
      if ($bytes[$dataOff + $i] -ne $pngMagic[$i]) { $isPng = $false; break }
    }
    if ($isPng) {
      $payload = New-Object byte[] $dataLen
      [Array]::Copy($bytes, $dataOff, $payload, 0, $dataLen)
      $ms = New-Object System.IO.MemoryStream(,$payload)
      try {
        $img = [System.Drawing.Image]::FromStream($ms)
        Write-Host ("    found {0}: {1}x{2}" -f $type, $img.Width, $img.Height)
        if ($img.Width -gt $bestPx) { $bestPx = $img.Width; $best = $img }
        else { $img.Dispose() }
      } catch { }
    }
  }
  $pos += $len
}

if (-not $best) { Write-Host 'FAIL: no PNG variant inside the icns' -ForegroundColor Red; exit 1 }
Write-Host ("==> master artwork: {0}x{1}" -f $best.Width, $best.Height)
if ($bestPx -lt 256) {
  Write-Host "FAIL: largest embedded icon is ${bestPx}px; NSIS needs 256" -ForegroundColor Red; exit 1
}

# --- build a multi-resolution .ico --------------------------------------- #
$sizes = @(16, 32, 48, 64, 128, 256)
$blobs = @()
foreach ($s in $sizes) {
  $bmp = New-Object System.Drawing.Bitmap($s, $s, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.InterpolationMode  = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g.PixelOffsetMode    = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
  $g.SmoothingMode      = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
  $g.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
  $g.Clear([System.Drawing.Color]::Transparent)
  $g.DrawImage($best, 0, 0, $s, $s)
  $g.Dispose()
  $ms = New-Object System.IO.MemoryStream
  $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
  $blobs += ,@($s, $ms.ToArray())
  $bmp.Dispose(); $ms.Dispose()
}
$best.Dispose()

$out = New-Object System.IO.MemoryStream
$w = New-Object System.IO.BinaryWriter($out)
$w.Write([UInt16]0)                 # reserved
$w.Write([UInt16]1)                 # type 1 = icon
$w.Write([UInt16]$blobs.Count)
# Directory entries come first, so every offset must account for the whole
# directory even though the payloads are appended afterwards.
$offset = 6 + (16 * $blobs.Count)
foreach ($b in $blobs) {
  $size = $b[0]; $data = $b[1]
  # 256 is encoded as 0 in a single byte — the field cannot hold 256 itself.
  $w.Write([byte]($(if ($size -ge 256) { 0 } else { $size })))   # width
  $w.Write([byte]($(if ($size -ge 256) { 0 } else { $size })))   # height
  $w.Write([byte]0)                 # palette count
  $w.Write([byte]0)                 # reserved
  $w.Write([UInt16]1)               # colour planes
  $w.Write([UInt16]32)              # bits per pixel
  $w.Write([UInt32]$data.Length)
  $w.Write([UInt32]$offset)
  $offset += $data.Length
}
foreach ($b in $blobs) { $w.Write($b[1]) }
$w.Flush()
[System.IO.File]::WriteAllBytes($IcoOut, $out.ToArray())
$w.Dispose(); $out.Dispose()

$kb = [math]::Round((Get-Item $IcoOut).Length / 1KB, 1)
Write-Host "OK: $IcoOut ($kb KB), sizes: $($sizes -join ', ')" -ForegroundColor Green

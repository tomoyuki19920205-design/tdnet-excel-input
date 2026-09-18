param([Parameter(Mandatory=$true)][string]$Directory)

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Storage.FileAccessMode,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Foundation,ContentType=WindowsRuntime]

$asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() |
  Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } |
  Select-Object -First 1
function Await-WinRt([object]$Operation, [Type]$ResultType) {
  $method = $asTaskGeneric.MakeGenericMethod($ResultType)
  $task = $method.Invoke($null, @($Operation)); $task.Wait(); return $task.Result
}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('ja'))
$pages = foreach ($item in Get-ChildItem -LiteralPath (Resolve-Path -LiteralPath $Directory).Path -Filter '*.png' | Sort-Object Name) {
  $file = Await-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync($item.FullName)) ([Windows.Storage.StorageFile])
  $stream = Await-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $words = foreach ($line in $result.Lines) { foreach ($word in $line.Words) {
    [pscustomobject]@{ Text=$word.Text; X=[double]$word.BoundingRect.X; Y=[double]$word.BoundingRect.Y; H=[double]$word.BoundingRect.Height }
  }}
  $ordered = New-Object System.Collections.Generic.List[object]
  foreach ($word in ($words | Sort-Object Y, X)) {
    $target = $null
    foreach ($row in $ordered) { if ([Math]::Abs($row.Y - $word.Y) -le [Math]::Max(8, $word.H * 0.55)) { $target=$row; break } }
    if ($null -eq $target) { $target=[pscustomobject]@{Y=$word.Y; Words=(New-Object System.Collections.Generic.List[object])}; $ordered.Add($target) }
    $target.Words.Add($word)
  }
  $text = (($ordered | Sort-Object Y | ForEach-Object { (($_.Words | Sort-Object X | ForEach-Object Text) -join ' ') }) -join "`n")
  [pscustomobject]@{ name=$item.Name; text=$text }
}
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$pages | ConvertTo-Json -Compress

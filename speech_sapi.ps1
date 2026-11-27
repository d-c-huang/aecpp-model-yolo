param([Parameter(Mandatory=$true)][string]$RequestPath,
      [Parameter(Mandatory=$true)][string]$ResponsePath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Speech
$request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voices = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled })
    $chinese = @($voices | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh-*' })
    if ($chinese.Count -eq 0) { throw 'No Chinese system voice. Install a Chinese speech language pack in Windows settings.' }
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    $results = @()
    foreach ($line in $request.lines) {
        $voice = $chinese[[int]$line.profile % $chinese.Count].VoiceInfo.Name
        $synth.SelectVoice($voice)
        $synth.Rate = [int]$line.rate
        $synth.SetOutputToWaveFile([string]$line.path, $format)
        $synth.Speak([string]$line.text)
        $synth.SetOutputToNull()
        $results += @{ id=$line.id; voice=$voice; rate=$line.rate; path=$line.path }
        $json = @{ completed=$results.Count; total=$request.lines.Count; lines=@($results) } | ConvertTo-Json -Depth 6
        $temporary = $ResponsePath + '.tmp'
        [System.IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $temporary -Destination $ResponsePath -Force
    }
} finally { $synth.Dispose() }

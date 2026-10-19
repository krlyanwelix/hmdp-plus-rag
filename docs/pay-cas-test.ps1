param([string]$Token)
$Base="http://127.0.0.1:8081"; $tmp=Join-Path $env:TEMP "hmdp-smoke2"; New-Item -ItemType Directory -Force -Path $tmp|Out-Null
function Call([string]$m,[string]$p,[string]$j){
  $a=@('-s','-X',$m,"$Base$p",'-o',"$tmp\r.json",'-H',"authorization: $Token")
  if($j){ [System.IO.File]::WriteAllText("$tmp\q.json",$j,(New-Object System.Text.UTF8Encoding($false))); $a+=@('-H','Content-Type: application/json','--data-binary',"@$tmp\q.json") }
  & curl.exe @a|Out-Null
  [System.IO.File]::ReadAllText("$tmp\r.json",[System.Text.Encoding]::UTF8)
}
$r=Call 'POST' '/registration/1' $null
Write-Host "register => $r"
$id=($r|ConvertFrom-Json).data
Start-Sleep -Milliseconds 300
Write-Host ("pay      => " + (Call 'PUT' "/registration/pay/$id" $null))
Start-Sleep -Seconds 9
Write-Host ("after 9s => " + (Call 'GET' "/registration/result/$id" $null))
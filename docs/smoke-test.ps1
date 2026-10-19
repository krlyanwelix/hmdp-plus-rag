# 报名链路冒烟脚本（Windows PowerShell）
# 用法：
#   .\docs\smoke-test.ps1 -Code 123456        # 走登录
#   .\docs\smoke-test.ps1 -Token <token>      # 复用会话，反复执行
param(
    [string]$Code = "000000",
    [string]$Token = "",
    [string]$Base = "http://127.0.0.1:8081",
    [string]$Phone = "13800000000"
)

$ErrorActionPreference = 'Continue'
$tmp = Join-Path $env:TEMP "hmdp-smoke"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

function Call([string]$method, [string]$path, [string]$token, [string]$json) {
    $args = @('-s', '-X', $method, "$Base$path", '-o', "$tmp\resp.json")
    if ($token) { $args += @('-H', "authorization: $token") }
    if ($json) {
        [System.IO.File]::WriteAllText("$tmp\req.json", $json, (New-Object System.Text.UTF8Encoding($false)))
        $args += @('-H', 'Content-Type: application/json; charset=utf-8', '--data-binary', "@$tmp\req.json")
    }
    & curl.exe @args | Out-Null
    return [System.IO.File]::ReadAllText("$tmp\resp.json", [System.Text.Encoding]::UTF8)
}

function Step([string]$title, [string]$resp) {
    Write-Host ""
    Write-Host "== $title" -ForegroundColor Cyan
    Write-Host "   $resp"
}

function WaitLimit() { Start-Sleep -Milliseconds 1300 }

# 1. 登录（传了 -Token 就跳过，省一个验证码）
if ($Token) {
    $token = $Token
    Step '1 复用会话' $token
} else {
    $login = Call 'POST' '/user/login' $null (@{ phone = $Phone; code = $Code } | ConvertTo-Json -Compress)
    Step '1 登录' $login
    $token = ($login | ConvertFrom-Json).data
    if (-not $token) { Write-Host "登录失败，检查验证码" -ForegroundColor Red; exit 1 }
}

# 1b. 清掉上一轮遗留的待支付/已支付单，否则"一人一赛"会挡住后面的步骤
$mine = (Call 'GET' '/registration/my' $token $null) | ConvertFrom-Json
foreach ($r in @($mine.data)) {
    if ($null -ne $r -and ($r.status -eq 1 -or $r.status -eq 2)) {
        WaitLimit
        Step "1b 清理遗留单 $($r.id)" (Call 'PUT' "/registration/cancel/$($r.id)" $token $null)
    }
}
WaitLimit

# 2. 实名档案
Step '2 实名档案' (Call 'POST' '/athlete' $token (@{ realName = '张三'; idCard = '440101199001011234'; gender = 1; birthday = '1990-01-01'; bestTimeSeconds = 3400 } | ConvertTo-Json -Compress))
WaitLimit

# 3. 报名 -> 等超时关单（--registration.delay-level=1 时是 5 秒；默认 14 档是 10 分钟）
$r1 = Call 'POST' '/registration/1' $token $null
Step '3 报名(半程马拉松)' $r1
$id1 = ($r1 | ConvertFrom-Json).data
Start-Sleep -Seconds 9
Step '3b 超时消息到达后（期望 status=4 已取消）' (Call 'GET' "/registration/result/$id1" $token $null)
WaitLimit

# 4. 报名 -> 立刻支付 -> 用同样的方式等超时消息，验证 CAS 不会把已支付单关掉
$r2 = Call 'POST' '/registration/2' $token $null
$id2 = ($r2 | ConvertFrom-Json).data
Step '4 报名(欢乐跑)' $r2
WaitLimit
Step '4b 支付' (Call 'PUT' "/registration/pay/$id2" $token $null)
Start-Sleep -Seconds 9
Step '4c 超时消息到达后（期望仍是 status=2 已支付）' (Call 'GET' "/registration/result/$id2" $token $null)
WaitLimit

# 5. 取消 -> 同赛事换组别重报 -> 再取消（历史失效单为 NULL，唯一索引不冲突）
$r3 = Call 'POST' '/registration/3' $token $null
$id3 = ($r3 | ConvertFrom-Json).data
Step '5 报名(越野30)' $r3
WaitLimit
Step '5b 取消' (Call 'PUT' "/registration/cancel/$id3" $token $null)
WaitLimit
$r4 = Call 'POST' '/registration/4' $token $null
$id4 = ($r4 | ConvertFrom-Json).data
Step '5c 同赛事换组别重报' $r4
WaitLimit
Step '5d 再次取消（active_flag 用 0 时这里会撞唯一索引）' (Call 'PUT' "/registration/cancel/$id4" $token $null)
WaitLimit

# 6. 售罄：直接把 Redis 余量打成 0，Lua 应当返回 1
& "E:\redis\redis-cli.exe" set "quota:group:1" 0 | Out-Null
Step '6 名额售罄' (Call 'POST' '/registration/1' $token $null)
& "E:\redis\redis-cli.exe" del "quota:group:1" | Out-Null
WaitLimit

# 7. 我的报名
Step '7 我的报名' (Call 'GET' '/registration/my' $token $null)

Write-Host ""
Write-Host "token=$token" -ForegroundColor DarkGray

@echo off
setlocal
cd /d %~dp0
chcp 65001 >nul

set "CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "NODE_PATH=D:\Program Files\nodejs\node.exe"
set "PROFILE_DIR=%~dp0.chrome-ocr-profile"
set "FORCE_RESTART=1"
if /I "%~1"=="--keep" set "FORCE_RESTART=0"

if not exist "%NODE_PATH%" for /f "delims=" %%i in ('where node 2^>nul') do set "NODE_PATH=%%i"
if not exist "%NODE_PATH%" goto :err_node

echo [1/5] Ensure Chrome debugging port 9222...
powershell -NoProfile -ExecutionPolicy Bypass -Command "if ((Test-NetConnection 127.0.0.1 -Port 9222 -WarningAction SilentlyContinue).TcpTestSucceeded) { Write-Host 'Chrome 9222 already alive' } elseif (Test-Path '%CHROME_PATH%') { Start-Process -FilePath '%CHROME_PATH%' -ArgumentList '--remote-debugging-port=9222','--user-data-dir=%PROFILE_DIR%' } else { exit 2 }"
if errorlevel 2 (
  echo [ERROR] Chrome not found: %CHROME_PATH%
  pause
  exit /b 1
)

if "%FORCE_RESTART%"=="1" (
  call :restart_services
) else (
  echo [2/5] Keep existing service processes...
)

echo [3/5] Start price-agent (mobile APIs) on 7788...
start "trips-price-agent-mobile" /B "%NODE_PATH%" "%~dp0price-agent.js" --source=mobile --port=7788 1>>"%~dp0logs-price-agent-7788.txt" 2>>&1

echo [4/5] Start price-agent (browser tabs) on 7789...
start "trips-price-agent-browser" /B "%NODE_PATH%" "%~dp0price-agent.js" --source=browser --port=7789 1>>"%~dp0logs-price-agent-7789.txt" 2>>&1

echo [5/5] Start service-hub on 7799...
start "trips-service-hub" /B "%NODE_PATH%" "%~dp0service-hub.js" 1>>"%~dp0logs-service-hub-7799.txt" 2>>&1

echo Health check...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$fail=0; $ports=@(7788,7789,7799); foreach($p in $ports){$ok=$false; for($i=0;$i -lt 10;$i++){try{$r=Invoke-RestMethod ('http://127.0.0.1:'+ $p +'/health') -TimeoutSec 2; if($r.ok){$ok=$true; break}}catch{}; Start-Sleep -Milliseconds 500}; if($ok){Write-Host ('[OK] http://127.0.0.1:'+ $p +'/health')} else {Write-Host ('[FAIL] http://127.0.0.1:'+ $p +'/health'); $fail=1}}; exit $fail"
if errorlevel 1 goto :err_health

echo Done.
echo Frontend index: http://127.0.0.1:7799/index.html
echo Frontend admin: http://127.0.0.1:7799/admin.html
echo price-agent mobile API: http://127.0.0.1:7788
echo Browser tab fetch API: http://127.0.0.1:7789
echo service-hub: http://127.0.0.1:7799
if /I "%~1"=="--no-pause" exit /b 0
echo.
echo Press any key to close this window...
pause >nul
exit /b 0

:err_node
echo [ERROR] Node not found. Please install Node.js or fix NODE_PATH.
pause
exit /b 1

:restart_services
echo [2/5] Restart service processes (7788/7789/7799)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ports=@(7788,7789,7799); foreach($p in $ports){$c=Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if($c){Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue}}; Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^node(\\.exe)?$' -and $_.CommandLine -match 'price-agent\\.js|service-hub\\.js' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 1 /nobreak >nul
goto :eof

:err_health
echo [ERROR] One or more services failed health check.
echo Please see logs:
echo   - %~dp0logs-price-agent-7788.txt
echo   - %~dp0logs-price-agent-7789.txt
echo   - %~dp0logs-service-hub-7799.txt
pause
exit /b 1

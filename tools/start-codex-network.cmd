@echo off
setlocal
set "AUTOCODING_CODEX=%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe"
if not exist "%AUTOCODING_CODEX%" (
  echo Codex executable not found at the expected installation path.
  exit /b 2
)
"%AUTOCODING_CODEX%" -C "%~dp0.." --sandbox workspace-write --ask-for-approval on-request -c sandbox_workspace_write.network_access=true
exit /b %ERRORLEVEL%

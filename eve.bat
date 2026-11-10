@echo off
rem Runs the market tool with the project virtualenv, no activation needed:  eve collect, eve login, ...
pushd "%~dp0"
".venv\Scripts\python.exe" -m eve_market %*
set rc=%errorlevel%
popd
exit /b %rc%

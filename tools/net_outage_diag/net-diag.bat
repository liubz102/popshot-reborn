@echo off
rem Network outage diagnostics for players (bug investigation 29). All text lives in net-diag.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0net-diag.ps1" %*

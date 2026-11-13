@echo off
rem Windows launcher: runs the Python script next to this file.
where py >nul 2>nul && (py -3 "%~dp0evenfall" %*) || (python "%~dp0evenfall" %*)

@echo off
rem Wrapper so the converter can be called as: pub2pdf <path> [options]
py "%~dp0pub2pdf.py" %*

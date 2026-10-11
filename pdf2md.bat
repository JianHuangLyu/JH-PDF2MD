@echo off
rem Drag PDF files or folders onto this file to convert them to Markdown.
rem Command line: pdf2md.bat file.pdf [options]
python "%~dp0pdf2md.py" %*
echo.
pause

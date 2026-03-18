@echo off
setlocal

echo =========================
echo Building iCSA-iVSA Compensation Build Tool
echo =========================
echo.

if exist venv rmdir /s /q venv
py -m venv venv
call venv\Scripts\activate.bat

python -m pip install --upgrade pip
pip install -r requirements.txt

pyinstaller iCSA-iVSA_Compensation_Build_Tool.spec

echo.
echo Build complete.
echo Check the dist folder.
echo.

endlocal
pause
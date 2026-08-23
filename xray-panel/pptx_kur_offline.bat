@echo off
rem PPTX cikti ozelligi icin python-pptx paketini internetsiz (wheels klasorunden) kurar.
rem Panel bu paket olmadan da calisir; sadece "PPTX indir" dugmesi icin gereklidir.
cd /d "%~dp0"
python -m pip install --no-index --find-links wheels python-pptx
pause

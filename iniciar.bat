@echo off
setlocal
cd /d "%~dp0"
title DuckMiner - servidor
if not exist "public\online.js" goto missing
if not exist "catalog.json" goto missing
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
  if not errorlevel 1 (
    py -3 server.py --open
    goto done
  )
)
where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
  if not errorlevel 1 (
    python server.py --open
    goto done
  )
)
echo Python 3.11 ou mais recente nao foi encontrado.
echo Instale pelo endereco https://www.python.org/downloads/windows/
echo Marque Add Python to PATH. Depois execute este arquivo novamente.
goto done
:missing
echo Extraia o ZIP inteiro antes de abrir o jogo.
echo O iniciar.bat deve ficar junto do server.py, catalog.json e pasta public.
:done
echo.
echo Para jogar, mantenha esta janela aberta.
echo Se ocorreu um erro acima, copie o texto e envie no chat.
pause

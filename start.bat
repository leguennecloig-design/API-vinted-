@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Vinted Niche Watch

rem --- Python present ? ---
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo [ERREUR] Python est introuvable. Installe-le depuis https://www.python.org/downloads/
  echo          en cochant "Add python.exe to PATH", puis relance start.bat.
  pause
  exit /b 1
)

rem --- Environnement virtuel ---
if not exist ".venv\Scripts\python.exe" (
  echo Creation de l'environnement Python ^(.venv^)...
  %PY% -m venv .venv
  if errorlevel 1 ( echo [ERREUR] Impossible de creer .venv & pause & exit /b 1 )
)

rem --- Dependances (reinstallees si requirements.txt change) ---
fc /b requirements.txt ".venv\installed.txt" >nul 2>nul
if errorlevel 1 (
  echo Installation des dependances...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 ( echo [ERREUR] Installation des dependances echouee & pause & exit /b 1 )
  copy /y requirements.txt ".venv\installed.txt" >nul
)

rem --- Lancement : le navigateur s'ouvre tout seul ---
set VNW_OPEN_BROWSER=1
echo.
echo Demarrage du serveur... (ferme cette fenetre ou Ctrl+C pour arreter)
".venv\Scripts\python.exe" -m app.main
pause

@echo off
rem Modo VISIVEL (debug): mostra o log no console.
rem No dia a dia o bot roda invisivel na bandeja via DJLoulouBot.exe (inicia com o Windows).
title DJ Loulou Bot (modo visivel)
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo [ERRO] Ambiente virtual nao encontrado.
    echo Rode primeiro:  py -3.12 -m venv .venv  e depois  .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

.venv\Scripts\python.exe bot.py

pause

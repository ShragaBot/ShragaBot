@echo off
title Shraga Setup
cd /d "C:\Dev\shraga-worker"
git pull
powershell -ExecutionPolicy Bypass -File "C:\Dev\shraga-worker\setup-devbox.ps1" %*

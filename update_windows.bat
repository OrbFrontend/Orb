@echo off

cd /d "%~dp0"

where git >nul 2>&1
if errorlevel 1 (
    echo Error: git is not installed. Install it from https://git-scm.com/downloads, then run this script again.
    pause
    exit /b 1
)

set "REPO_URL=https://github.com/OrbFrontend/Orb.git"

rem One block, parsed before it runs: cmd rereads this file between commands and the update rewrites it.
(
    if exist ".git" (
        for /f "tokens=*" %%b in ('git rev-parse --abbrev-ref HEAD') do (
            echo Current branch: %%b
            echo Pulling latest changes...
            echo.
            git pull origin %%b
            if errorlevel 1 (
                echo.
                echo Error: update failed.
                pause
                exit /b 1
            )
        )
    ) else (
        echo No git history found. Converting this folder to a git install...
        echo.
        git init -q
        git symbolic-ref HEAD refs/heads/main
        git remote add origin "%REPO_URL%"
        git fetch origin main
        if errorlevel 1 (
            rmdir /s /q .git
            echo.
            echo Error: could not download Orb from GitHub. Check your connection and run this again.
            pause
            exit /b 1
        )
        git reset -q --hard origin/main
        git branch -q --set-upstream-to=origin/main main
        rem Files the zip shipped that Orb has since deleted; migrations and plug-ins load by directory scan.
        for /f "delims=" %%f in ('git -c core.quotePath^=false ls-files --others --exclude-standard') do (
            for /f %%d in ('git --literal-pathspecs log -1 --no-renames --diff-filter^=D --oneline origin/main -- "%%f"') do (
                git --literal-pathspecs clean -fq -- "%%f"
            )
        )
    )
    echo.
    echo Update complete.
    pause
    exit /b 0
)

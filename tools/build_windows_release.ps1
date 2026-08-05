param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^v\d+\.\d+\.\d+(?:-rc\.\d+)?$')]
    [string]$Version
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-NativeSuccess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Operation,
        [Parameter(Mandatory = $true)]
        [int]$ExitCode
    )
    if ($ExitCode -ne 0) {
        throw "$Operation failed with exit code $ExitCode"
    }
}

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$DistRoot = Join-Path $Root "dist"
$BuildRoot = Join-Path $Root "build"
$PackageName = "internal-upload_${Version}_windows"
$PackageRoot = Join-Path $DistRoot $PackageName
$PyInstallerDist = Join-Path $BuildRoot "pyinstaller-dist"
$ServerWork = Join-Path $BuildRoot "server-work"
$ClientWork = Join-Path $BuildRoot "client-work"
$ZipPath = Join-Path $DistRoot "$PackageName.zip"
$ShaPath = "$ZipPath.sha256"
$ReleaseNotesPath = Join-Path $DistRoot "release_notes_$Version.md"
$ServerVersionInfo = Join-Path $BuildRoot "server-version.txt"
$ClientVersionInfo = Join-Path $BuildRoot "client-version.txt"
$TemplatesPath = Join-Path $Root "templates"
$StaticPath = Join-Path $Root "static"

foreach ($Path in @($PackageRoot, $PyInstallerDist, $ServerWork, $ClientWork, $ZipPath, $ShaPath, $ReleaseNotesPath)) {
    if (Test-Path $Path) { Remove-Item $Path -Recurse -Force }
}
New-Item -ItemType Directory -Force -Path $DistRoot, $BuildRoot, $PackageRoot, $PyInstallerDist, $ServerWork, $ClientWork | Out-Null

Push-Location $Root
try {
    $SourceVersionOutput = python -c "from app_version import APP_VERSION; print(APP_VERSION)"
    Assert-NativeSuccess "Source version lookup" $LASTEXITCODE
    $SourceVersion = $SourceVersionOutput.Trim()
    if ($SourceVersion -ne $Version) {
        throw "Source APP_VERSION $SourceVersion does not match requested release $Version"
    }
    $WorktreeStatus = git status --porcelain --untracked-files=all
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect Git worktree state" }
    if ($WorktreeStatus) {
        throw "Release builds require a clean Git worktree so security_manifest.json matches the source commit"
    }
    $SourceCommit = (git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $SourceCommit) {
        throw "Unable to resolve the source commit for the release"
    }

    python tools/generate_windows_version_info.py `
        --version $Version `
        --product-name "Internal Upload Server" `
        --description "Internal file upload and network measurement server" `
        --filename "InternalUploadServer.exe" `
        --output $ServerVersionInfo
    Assert-NativeSuccess "Server version metadata generation" $LASTEXITCODE
    python tools/generate_windows_version_info.py `
        --version $Version `
        --product-name "Network Probe Client" `
        --description "Internal TCP network measurement client" `
        --filename "NetworkProbeClient.exe" `
        --output $ClientVersionInfo
    Assert-NativeSuccess "Client version metadata generation" $LASTEXITCODE

    python -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --noupx `
        --name InternalUploadServer `
        --version-file $ServerVersionInfo `
        --distpath $PyInstallerDist `
        --workpath $ServerWork `
        --specpath $BuildRoot `
        --add-data "${TemplatesPath};templates" `
        --add-data "${StaticPath};static" `
        app.py
    Assert-NativeSuccess "Server executable build" $LASTEXITCODE

    python -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --noupx `
        --name NetworkProbeClient `
        --version-file $ClientVersionInfo `
        --distpath $PyInstallerDist `
        --workpath $ClientWork `
        --specpath $BuildRoot `
        probe_client.py
    Assert-NativeSuccess "Client executable build" $LASTEXITCODE

    $ServerBundle = Join-Path $PyInstallerDist "InternalUploadServer"
    $ClientBundle = Join-Path $PyInstallerDist "NetworkProbeClient"
    $ServerExe = Join-Path $ServerBundle "InternalUploadServer.exe"
    $ClientExe = Join-Path $ClientBundle "NetworkProbeClient.exe"
    if (-not (Test-Path $ServerExe)) { throw "PyInstaller did not create $ServerExe" }
    if (-not (Test-Path $ClientExe)) { throw "PyInstaller did not create $ClientExe" }

    Copy-Item (Join-Path $ServerBundle "*") $PackageRoot -Recurse
    $ClientTemplate = Join-Path $PackageRoot "client-template"
    New-Item -ItemType Directory -Force -Path $ClientTemplate | Out-Null
    Copy-Item (Join-Path $ClientBundle "*") $ClientTemplate -Recurse

    Copy-Item "config.ini" (Join-Path $PackageRoot "config.ini")
    Copy-Item "README.md" (Join-Path $PackageRoot "README.md")
    Copy-Item "RELEASE_NOTES.md" (Join-Path $PackageRoot "RELEASE_NOTES.md")
    Copy-Item "CHANGELOG.md" (Join-Path $PackageRoot "CHANGELOG.md")

    New-Item -ItemType Directory -Force -Path `
        (Join-Path $PackageRoot "data"), `
        (Join-Path $PackageRoot "data/network_check_results"), `
        (Join-Path $PackageRoot "data/network_probe_results"), `
        (Join-Path $PackageRoot "uploads") | Out-Null
    Copy-Item "data/upload_log.csv" (Join-Path $PackageRoot "data/upload_log.csv")
    Copy-Item "data/network_check_log.csv" (Join-Path $PackageRoot "data/network_check_log.csv")
    Copy-Item "data/network_check_session_log.csv" (Join-Path $PackageRoot "data/network_check_session_log.csv")
    Copy-Item "data/network_check_results/README_RESULTS_KO.txt" (Join-Path $PackageRoot "data/network_check_results/README_RESULTS_KO.txt")
    Copy-Item "data/network_probe_log.csv" (Join-Path $PackageRoot "data/network_probe_log.csv")
    Copy-Item "data/network_probe_results/README_RESULTS_KO.txt" (Join-Path $PackageRoot "data/network_probe_results/README_RESULTS_KO.txt")
    "업로드 파일이 저장되는 폴더입니다. 운영 중 생성된 파일은 GitHub에 올리지 마세요." | Set-Content -Path (Join-Path $PackageRoot "uploads/README_UPLOADS_KO.txt") -Encoding UTF8

    $LauncherPath = Join-Path $PackageRoot "start_internal_upload.cmd"
    $LauncherContent = @"
@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 사내 업로드 서버를 시작합니다.
echo.
echo 서버가 시작되면 콘솔에 실제 접속 주소가 표시됩니다.
echo 웹 또는 TCP 측정 포트가 사용 중이면 빈 포트로 변경할지 물어봅니다.
echo 승인된 포트는 config.ini에 자동 저장됩니다.
echo Windows 방화벽은 자동 조회하거나 변경하지 않습니다.
echo 종료하려면 이 창에서 Ctrl+C를 누르세요.
echo.
InternalUploadServer.exe
pause
"@
    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($LauncherPath, $LauncherContent, $Utf8NoBom)

    @"
사내 업로드 $Version Windows 포터블 폴더 ZIP

서버 실행:
1. ZIP을 Windows 서버 PC의 원하는 폴더에 완전히 압축 해제합니다.
2. start_internal_upload.cmd를 더블클릭합니다.
3. 콘솔에 표시된 실제 접속 주소를 브라우저에서 엽니다. 기본 웹 포트는 8000입니다.
4. 포트 충돌 시 프로그램이 제안한 빈 포트를 승인하면 config.ini에 저장됩니다.
5. Windows 방화벽은 자동 조회하거나 변경하지 않습니다. 다른 PC 접속이 실패하면 표시된 포트를 확인하세요.

운영 안정성:
- 같은 data 폴더를 사용하는 서버는 하나만 실행됩니다.
- 중단된 업로드·삭제와 HTTP/TCP 결과 저장은 다음 시작 때 트랜잭션 기록으로 복구합니다.
- 복구할 수 없는 충돌은 정상으로 추정하지 않고 안정된 오류 코드로 시작을 중단합니다.
- 시작 시 CSV 끝이 불완전하면 원본 .bak 파일을 남긴 뒤 마지막 레코드만 복구합니다.
- 진단 로그는 data/diagnostics에 2MB 단위로 순환 저장됩니다.
- 파일 업로드는 최대 4건을 처리하고 남은 예상 용량을 합산해 디스크 공간을 예약합니다.
- 서버는 최소 1GB의 디스크 여유 공간을 남기고 부족하면 업로드를 중단합니다.
- 웹 요청은 최대 32개를 처리하며 30초 동안 데이터가 없는 연결을 종료합니다.
- Ctrl+C 종료 시 새 요청을 차단하고 진행 중인 요청을 최대 30초 기다립니다.
- Windows의 파일·CSV·JSON·설정 교체는 디스크 반영을 기다리는 write-through 방식입니다.
- 측정 CSV는 오래된 행을 월별로 보관하고 상세 JSON은 유형별 최신 1,000건을 유지합니다.
- 웹 첫 화면의 운영 요약은 최근 완료·취소·실패, 부분 장애와 권장 조치를 민감정보 없이 표시합니다.

TCP 전송 성능 측정:
1. TCP 측정 서버는 기본으로 함께 시작됩니다. 기본 포트는 5201입니다.
2. 웹 화면의 TCP 전송 성능 측정에서 Windows 클라이언트 ZIP을 받습니다.
3. 측정 PC에서 ZIP 전체를 압축 해제하고 NetworkProbeClient.exe를 실행합니다.
4. 웹 화면에서 자동 등록된 PC를 선택해 측정합니다. 클라이언트 콘솔은 측정 중 열어 두세요.
5. 서버 IP 또는 웹 포트가 바뀌면 클라이언트 ZIP을 다시 받습니다.

보안 정보:
- 서버와 클라이언트는 기능이 분리된 별도 실행 파일입니다.
- 서버 시작 과정에서 PowerShell을 실행하지 않습니다.
- 실행파일, 스크립트, 매크로 문서와 디스크 이미지는 업로드할 수 없습니다.
- 압축파일 내부 검사와 파일 크기 제한은 적용하지 않습니다.
- 코드서명은 적용하지 않았습니다. SECURITY_REVIEW_KO.md와 SHA256SUMS.txt를 확인하세요.
"@ | Set-Content -Path (Join-Path $PackageRoot "README_START_HERE_KO.txt") -Encoding UTF8

    $PackagedServerExe = Join-Path $PackageRoot "InternalUploadServer.exe"
    $PackagedClientExe = Join-Path $ClientTemplate "NetworkProbeClient.exe"
    & $PackagedServerExe --smoke-check
    if ($LASTEXITCODE -ne 0) { throw "Server smoke check failed" }
    & $PackagedServerExe --probe-self-check
    if ($LASTEXITCODE -ne 0) { throw "Server probe self-check failed" }
    & $PackagedClientExe --self-check
    if ($LASTEXITCODE -ne 0) { throw "Client self-check failed" }

    $RuntimeLock = Join-Path $PackageRoot "data/.internal-upload.instance.lock"
    if (Test-Path $RuntimeLock) { Remove-Item $RuntimeLock -Force }
    $RuntimeDiagnostics = Join-Path $PackageRoot "data/diagnostics"
    if (Test-Path $RuntimeDiagnostics) { Remove-Item $RuntimeDiagnostics -Recurse -Force }

    python tools/generate_security_artifacts.py `
        --root $PackageRoot `
        --version $Version `
        --source-commit $SourceCommit `
        --requirements-lock (Join-Path $Root "requirements-windows.lock")
    Assert-NativeSuccess "Security artifact generation" $LASTEXITCODE

    Compress-Archive -Path (Join-Path $PackageRoot "*") -DestinationPath $ZipPath -Force
    python tools/verify_release_zip.py --zip $ZipPath --version $Version
    Assert-NativeSuccess "Release ZIP verification" $LASTEXITCODE

    $Hash = (Get-FileHash $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    [System.IO.File]::WriteAllText($ShaPath, "$Hash  $PackageName.zip`n", [System.Text.Encoding]::ASCII)
    if ([System.IO.File]::ReadAllBytes($ShaPath) -contains 13) {
        throw "SHA256 file must use LF line endings"
    }

    @"
# $Version - 사내 업로드 사용성 및 안정성 개선

## 주요 변경

- 업로드·삭제의 파일/CSV 경계와 HTTP/TCP 측정의 JSON/CSV 경계에 durable transaction과 재시작 복구 적용
- 손상·충돌 transaction과 미정리 marker는 정상으로 추정하지 않고 새 작업 전에 fail-closed
- 0바이트, 불일치 합계, 빈 결과와 변경된 응답 형식을 성공으로 저장하지 않도록 결과 검증 강화
- 네트워크·요청 시간 상한, 동시 실행 제한, 중복 실행 방지와 취소·종료 자원 정리 강화
- 결과 JSON 삭제 경합·손상·인코딩 오류를 경로와 traceback 없는 ``RESULT_READ_FAILED``로 반환
- 설정·권한·저장 실패를 안정된 한국어 오류 코드와 다음 조치로 안내
- 최근 완료·취소·실패, 부분 장애와 권장 조치를 민감정보 없이 보여주는 관리자 운영 요약 추가
- 회전 진단 로그, 상태 API 실패 counter, 기본 config와 header-only 운영 CSV 릴리스 검사 추가
- Windows 반복 시험에 working set·handle·thread·TCP socket 계측과 독립 누수 분석기 추가
- 현재 소스 근거, 사용자별 평가와 P0/P1/P2 계획을 담은 한국어 진단 보고서 추가
- Windows PowerShell 5.1 한국어와 CMD 실행을 각각 UTF-8 BOM/no-BOM으로 고정하고 local/Actions native 실패를 즉시 전파
- Actions checkout 뒤 원격 tag ref를 다시 받아 annotated tag object와 source commit 일치를 검증
- 이전 ``v0.5.1`` tag workflow는 Windows 비동기 timeout 테스트 경합으로 중단됐고 Release asset은 생성되지 않음
- TCP timeout 테스트는 ``persistence_complete``까지 기다리며, 저장 후 gate 해제와 완료 플래그 공개를 같은 임계구역에서 처리
- 기존 업로드·다운로드 URL, CSV·JSON·Excel 형식과 TCP 프로토콜 ``v2`` 유지

## 검증

- 전체 회귀 458건과 장애 주입 32건 통과
- Python compileall, JavaScript 5개 구문 검사와 의존성 무결성 검사 통과
- Windows 2,708.89초 반복 시험 386 cycles 분석 결과 ``PASS_NO_REPEATED_PROCESS_GROWTH``
- 서버 smoke·TCP 자체 점검, 클라이언트 자체 점검, 보안 산출물과 ZIP verifier 통과

## 실행

1. ``$PackageName.zip``을 완전히 압축 해제합니다.
2. ``start_internal_upload.cmd``를 실행합니다.
3. TCP 측정 PC에서는 서버 웹 화면에서 클라이언트 ZIP을 받고 ``NetworkProbeClient.exe``를 실행합니다.

## 보안상 제한

- 코드서명은 적용하지 않았으므로 보안 제품 경고가 완전히 사라지는 것을 보장하지 않습니다.
- HTTP/TCP 토큰과 데이터는 평문이며 요청 Host 기반 주소 생성, 사내망 무인증 접근, 파일 크기 무제한, 압축파일 내부 미검사와 TCP 장기 폴링은 유지됩니다.
- Windows 방화벽은 자동 조회하거나 변경하지 않습니다.

SHA256: ``$Hash``
"@ | Set-Content -Path $ReleaseNotesPath -Encoding UTF8

    Write-Host "Built $ZipPath"
    Write-Host "SHA256 $Hash"
}
finally {
    Pop-Location
}

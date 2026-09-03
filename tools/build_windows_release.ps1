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
$SbomPath = Join-Path $DistRoot "internal-upload_${Version}_sbom.cdx.json"
$ReleaseNotesPath = Join-Path $DistRoot "release_notes_$Version.md"
$ServerVersionInfo = Join-Path $BuildRoot "server-version.txt"
$ClientVersionInfo = Join-Path $BuildRoot "client-version.txt"
$TemplatesPath = Join-Path $Root "templates"
$StaticPath = Join-Path $Root "static"

foreach ($Path in @($PackageRoot, $PyInstallerDist, $ServerWork, $ClientWork, $ZipPath, $ShaPath, $SbomPath, $ReleaseNotesPath)) {
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
    Copy-Item "LICENSE" (Join-Path $PackageRoot "LICENSE")

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
- 웹 로그인 토큰은 사용하지 않습니다. 서버 웹 포트에 도달 가능한 내부망 사용자는 화면을 바로 열 수 있습니다.
- 브라우저의 상태 변경 요청은 CSRF 토큰을 검증하고 TCP 클라이언트 등록은 짧은 수명의 일회용 enrollment token을 유지합니다.
- TCP 제어 프레임은 HMAC과 nonce로 재전송을 차단합니다. 웹 접근 범위는 Windows 방화벽, VLAN/ACL 또는 VPN으로 제한하세요.
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
    Copy-Item (Join-Path $PackageRoot "sbom.cdx.json") $SbomPath

    Compress-Archive -Path (Join-Path $PackageRoot "*") -DestinationPath $ZipPath -Force
    python tools/verify_release_zip.py --zip $ZipPath --version $Version
    Assert-NativeSuccess "Release ZIP verification" $LASTEXITCODE

    $Hash = (Get-FileHash $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    [System.IO.File]::WriteAllText($ShaPath, "$Hash  $PackageName.zip`n", [System.Text.Encoding]::ASCII)
    if ([System.IO.File]::ReadAllBytes($ShaPath) -contains 13) {
        throw "SHA256 file must use LF line endings"
    }

    @"
# $Version - 내부망 파일 전송 및 네트워크 진단 보안 강화

## 주요 변경

- 웹 로그인 access token과 master Bearer 인증을 제거하고 내부망에서 서버 주소를 바로 열 수 있도록 변경
- 브라우저 상태 변경 요청의 CSRF 검증과 응답 보안 헤더는 유지
- TCP 제어 프레임을 프로토콜 ``v3`` HMAC-SHA256으로 인증하고 timestamp·nonce 재전송 방지 적용
- Windows 클라이언트 등록은 짧은 수명의 일회용 enrollment token을 계속 사용
- PR·main push 검증의 모든 native 명령 실패를 즉시 전파해 뒤 명령이 실패 코드를 덮는 false-green 제거
- Windows soak Python 출력을 UTF-8로 고정하고 Step Summary는 bounded Markdown만 게시하며 원시 JSON은 artifact로 보존
- 기존 트랜잭션 복구, bounded server, 결과 검증과 CSV·JSON·Excel 호환성 유지

## 검증

- GitHub-hosted Windows CI의 전체 회귀·장애 주입·native exit 전파 검증 통과
- Python compileall, JavaScript 구문 검사와 hash-pinned 의존성 무결성 검사 통과
- GitHub-hosted Windows 45분 합성 soak의 기능 결과와 분석 후처리 결과를 각각 검증하고 원시 JSON 보존
- 서버 smoke·TCP 자체 점검, 클라이언트 자체 점검, SBOM·보안 산출물과 ZIP verifier 통과

## 실행

1. ``$PackageName.zip``을 완전히 압축 해제합니다.
2. ``start_internal_upload.cmd``를 실행합니다.
3. TCP 측정 PC에서는 서버 웹 화면에서 클라이언트 ZIP을 받고 ``NetworkProbeClient.exe``를 실행합니다.

## 보안상 제한

- 코드서명은 적용하지 않았으므로 보안 제품 경고가 완전히 사라지는 것을 보장하지 않습니다.
- 내장 HTTP/TCP 전송은 암호화하지 않습니다. 신뢰할 수 있는 내부망·VPN 또는 TLS 역방향 프록시에서 사용하세요.
- 파일 크기 제한과 압축파일 내부 검사는 적용하지 않습니다.
- Windows 방화벽은 자동 조회하거나 변경하지 않습니다.

SHA256: ``$Hash``
"@ | Set-Content -Path $ReleaseNotesPath -Encoding UTF8

    Write-Host "Built $ZipPath"
    Write-Host "SHA256 $Hash"
}
finally {
    Pop-Location
}

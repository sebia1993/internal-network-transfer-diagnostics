from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


MAX_MARKDOWN_BYTES = 16 * 1024
MAX_FINDINGS = 10


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "invalid"
    if not isinstance(value, dict):
        return None, "invalid"
    return value, "available"


def _cell(value: Any) -> str:
    text = str(value if value is not None else "-")
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")[:200]


def render_markdown(
    summary: dict[str, Any] | None,
    analysis: dict[str, Any] | None,
    *,
    summary_state: str,
    analysis_state: str,
) -> str:
    source = summary or {}
    quality = analysis.get("data_quality", {}) if analysis else {}
    lines = [
        "## Windows stability soak",
        "",
        "| 구분 | 결과 |",
        "|---|---|",
        f"| 기능 실행 | {_cell(source.get('status') if summary else summary_state)} |",
        f"| 분석 후처리 | {_cell(analysis.get('verdict') if analysis else analysis_state)} |",
        f"| 데이터 품질 | {_cell(quality.get('status', '-'))} |",
        f"| 실행 시간(초) | {_cell(source.get('duration_seconds'))} |",
        f"| 완료 cycle | {_cell(source.get('completed_cycles'))} |",
        f"| 업로드 바이트 | {_cell(source.get('uploaded_bytes'))} |",
        f"| TCP 자체 점검 | {_cell(source.get('tcp_self_checks'))} |",
        "",
    ]
    findings = analysis.get("review_findings", []) if analysis else []
    if isinstance(findings, list) and findings:
        lines.extend(["### 검토 항목", ""])
        for item in findings[:MAX_FINDINGS]:
            if not isinstance(item, dict):
                continue
            lines.append(
                "- "
                + " / ".join(
                    _cell(item.get(field, "-"))
                    for field in ("stage", "metric", "kind", "observed")
                )
            )
        if len(findings) > MAX_FINDINGS:
            lines.append(f"- 추가 {len(findings) - MAX_FINDINGS}건은 artifact의 원시 분석 JSON을 확인하세요.")
        lines.append("")
    lines.extend(
        [
            "원시 `windows-soak-summary.json`, `windows-soak-analysis.json`과 이 요약은 workflow artifact에 보존됩니다.",
            "이 결과는 GitHub-hosted Windows runner의 합성 부하 검증이며 현장 네트워크 성과를 뜻하지 않습니다.",
            "",
        ]
    )
    text = "\n".join(lines)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_MARKDOWN_BYTES:
        text = encoded[: MAX_MARKDOWN_BYTES - 80].decode("utf-8", errors="ignore")
        text += "\n\n요약이 크기 제한에 맞게 잘렸습니다. 전체 자료는 artifact를 확인하세요.\n"
    return text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="bounded Windows soak Step Summary 생성")
    parser.add_argument("--summary", default="windows-soak-summary.json")
    parser.add_argument("--analysis", default="windows-soak-analysis.json")
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary, summary_state = _read_json(Path(args.summary))
    analysis, analysis_state = _read_json(Path(args.analysis))
    markdown = render_markdown(
        summary,
        analysis,
        summary_state=summary_state,
        analysis_state=analysis_state,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8", newline="\n")
    return 0 if summary_state == "available" and analysis_state == "available" else 2


if __name__ == "__main__":
    raise SystemExit(main())

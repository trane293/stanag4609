"""Dependency-free HTML rendering for FMV verification reports."""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stanag4609.verifier import FMVVerificationReport, VerificationFinding


def _text(value: object | None) -> str:
    return "—" if value is None else escape(str(value))


def _tag_paths(paths: tuple[tuple[int, ...], ...]) -> str:
    return escape(", ".join("/".join(str(tag) for tag in path) for path in paths))


def _finding_context(finding: VerificationFinding) -> str:
    values: list[str] = []
    if finding.program_number is not None:
        values.append(f"program {finding.program_number}")
    if finding.pid is not None:
        values.append(f"PID 0x{finding.pid:04X}")
    if finding.requirement is not None:
        values.append(finding.requirement)
    if finding.tags:
        values.append("tag" + ("s" if len(finding.tags) != 1 else "") + " " + ", ".join(
            str(tag) for tag in finding.tags
        ))
    if finding.first_offset is not None:
        offset = f"offset {finding.first_offset}"
        if finding.last_offset is not None and finding.last_offset != finding.first_offset:
            offset += f"-{finding.last_offset}"
        values.append(offset)
    return ", ".join(values)


def render_verification_html(
    report: FMVVerificationReport,
    *,
    title: str = "FMV verification report",
) -> str:
    """Render a portable, escaped HTML report with no external dependencies."""

    safe_title = escape(title)
    result = "PASS" if report.ok else "FAIL"
    result_class = "pass" if report.ok else "error"
    source = escape(report.source or "<stream>")
    stream_rows = "".join(
        "<tr>"
        f"<td>{stream.program_number}</td><td><code>0x{stream.pid:04X}</code></td>"
        f"<td>{escape(stream.kind)}</td><td><code>0x{stream.stream_type:02X}</code></td>"
        f"<td>{_text(stream.carriage)}</td><td>{_text(stream.codec)}</td>"
        f"<td>{stream.transport_packets:,}</td><td>{stream.pes_packets:,}</td>"
        f"<td>{stream.payload_bytes:,}</td>"
        "</tr>"
        for stream in report.streams
    ) or '<tr><td colspan="9" class="empty">No elementary streams discovered</td></tr>'

    metadata_sections: list[str] = []
    for metadata in report.st0601_streams:
        service = (
            "asynchronous"
            if metadata.metadata_service_id is None
            else f"service {metadata.metadata_service_id}"
        )
        tag_rows = "".join(
            "<tr>"
            f"<td>{tag.tag}</td><td>{_text(tag.name)}</td>"
            f"<td>{tag.packets_present:,}</td><td>{tag.occurrences:,}</td>"
            f"<td>{tag.zero_length_items:,}</td><td>{tag.decoding_issues:,}</td>"
            "</tr>"
            for tag in metadata.tags
        ) or '<tr><td colspan="6" class="empty">No local tags retained</td></tr>'
        if metadata.mismms_coverage is None:
            mismms = '<p class="profile-note">ST 0902 profile not evaluated</p>'
        else:
            coverage_rows = "".join(
                "<tr>"
                f'<td><span class="status {item.status.value}">{item.status.value}</span></td>'
                f"<td>{escape(item.requirement)}</td>"
                f"<td>{_tag_paths(item.tag_paths)}</td>"
                f"<td>{_text(item.last_seen)}</td>"
                f"<td>{_text(item.age_seconds)}</td>"
                "</tr>"
                for item in metadata.mismms_coverage
            )
            mismms = (
                '<h3>ST 0902 minimum-item population at stream end</h3>'
                '<div class="table-wrap"><table><thead><tr><th>Status</th>'
                "<th>Requirement</th><th>Tags</th><th>Last seen</th><th>Age (s)</th>"
                f"</tr></thead><tbody>{coverage_rows}</tbody></table></div>"
            )
        versions = ", ".join(str(version) for version in metadata.versions) or "—"
        metadata_sections.append(
            "<details open><summary>"
            f"Program {metadata.program_number} · PID 0x{metadata.pid:04X} · {service}"
            "</summary>"
            '<div class="detail-grid">'
            f"<span><b>Packets</b>{metadata.packets:,}</span>"
            f"<span><b>MISP timestamped</b>{metadata.timestamped_packets:,}</span>"
            f"<span><b>Versions</b>{versions}</span>"
            "<span><b>First MISP coordinate</b>"
            f"{_text(metadata.first_misp_timestamp_microseconds)}</span>"
            "<span><b>Last MISP coordinate</b>"
            f"{_text(metadata.last_misp_timestamp_microseconds)}</span>"
            f"<span><b>UTC converted</b>{metadata.utc_timestamped_packets:,}</span>"
            "<span><b>UTC unavailable</b>"
            f"{metadata.utc_conversion_unavailable_packets:,}</span>"
            f"<span><b>First UTC timestamp</b>{_text(metadata.first_utc_timestamp)}</span>"
            f"<span><b>Last UTC timestamp</b>{_text(metadata.last_utc_timestamp)}</span>"
            "<span><b>Maximum forward gap</b>"
            f"{_text(metadata.maximum_forward_gap_seconds)} s</span>"
            f"<span><b>Regressions</b>{metadata.timestamp_regressions:,}</span>"
            f"<span><b>Duplicate timestamps</b>{metadata.duplicate_timestamps:,}</span>"
            "<span><b>External context</b>"
            f"{metadata.context_provided_packets:,} / {metadata.packets:,} packets</span>"
            "<span><b>Birth-time validated</b>"
            f"{metadata.birth_timestamp_validated_packets:,} packets</span>"
            "<span><b>IMAP precision validated</b>"
            f"{metadata.imap_precision_validated_items:,} items</span>"
            "<span><b>VMTI context validated</b>"
            f"{metadata.vmti_context_validated_packets:,} packets</span>"
            "<span><b>Invalid/missing timestamps</b>"
            f"{metadata.invalid_or_missing_timestamp_packets:,}</span>"
            f"<span><b>Untracked items</b>{metadata.untracked_item_occurrences:,}</span>"
            "</div>"
            '<div class="table-wrap"><table><thead><tr><th>Tag</th><th>Name</th>'
            "<th>Packets present</th><th>Occurrences</th><th>ZLI</th><th>Decode issues</th>"
            f"</tr></thead><tbody>{tag_rows}</tbody></table></div>{mismms}</details>"
        )
    if not metadata_sections:
        metadata_sections.append('<p class="empty panel">No ST 0601 services decoded</p>')

    finding_rows = "".join(_render_finding(finding) for finding in report.findings)
    if not finding_rows:
        finding_rows = '<tr><td colspan="5" class="empty">No findings emitted</td></tr>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>
:root {{ color-scheme: light dark; --bg:#0b1020; --panel:#131a2b; --line:#29324a;
  --text:#eef2ff; --muted:#aeb8d0; --pass:#43d17a; --warn:#f5bd4f; --error:#ff6677;
  --na:#8f9bb8; --accent:#72a7ff; }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5 ui-sans-serif,
  system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif }}
main {{ width:min(1180px,calc(100% - 32px)); margin:40px auto 72px }}
h1 {{ margin:0 0 6px; font-size:clamp(26px,4vw,42px); letter-spacing:-.03em }}
h2 {{ margin:34px 0 12px; font-size:20px }}
h3 {{ margin:20px 16px 8px; font-size:15px }}
.subhead {{ color:var(--muted); overflow-wrap:anywhere }}
.result {{ display:inline-block; margin-left:8px; padding:3px 10px; border:1px solid currentColor;
  border-radius:999px; font-size:13px; vertical-align:middle }}
.result.pass,.status.pass {{ color:var(--pass) }}
.result.error,.status.error {{ color:var(--error) }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px;
  margin:24px 0 }}
.card,.panel,details {{ background:var(--panel); border:1px solid var(--line); border-radius:12px }}
.card {{ padding:16px }} .card b {{ display:block; font-size:24px }}
.card span {{ color:var(--muted) }}
.table-wrap {{ overflow:auto }} table {{ width:100%; border-collapse:collapse; min-width:680px }}
th,td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:left;
  vertical-align:top }}
th {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em }}
tbody tr:last-child td {{ border-bottom:0 }} code {{ color:var(--accent) }}
details {{ margin:10px 0; overflow:hidden }}
summary {{ cursor:pointer; padding:14px 16px; font-weight:650 }}
.detail-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px;
  padding:0 16px 14px }} .detail-grid span {{ color:var(--muted) }}
.detail-grid b {{ display:block; color:var(--text); font-size:12px }}
.status {{ font-weight:750; text-transform:uppercase }} .status.warning {{ color:var(--warn) }}
.status.not_applicable {{ color:var(--na) }} .message {{ min-width:310px }}
.status.current {{ color:var(--pass) }} .status.missing,.status.overdue {{ color:var(--error) }}
.context {{ color:var(--muted) }}
.profile-note {{ color:var(--muted); margin:16px }}
.empty {{ color:var(--muted); text-align:center }} p.panel {{ padding:20px }}
footer {{ margin-top:30px; color:var(--muted); font-size:12px }}
@media print {{ :root {{ color-scheme:light; --bg:#fff; --panel:#fff; --line:#d9dde7;
  --text:#111827; --muted:#4b5563; --accent:#1d4ed8 }}
  main {{ width:100%; margin:0 }} details {{ break-inside:avoid }} }}
</style>
</head>
<body><main>
<header><h1>{safe_title}<span class="result {result_class}">{result}</span></h1>
<div class="subhead">Source: {source}</div></header>
<section class="cards" aria-label="Summary">
<div class="card"><b>{report.bytes_read:,}</b><span>bytes read</span></div>
<div class="card"><b>{report.transport_packets:,}</b><span>transport packets</span></div>
<div class="card"><b>{report.klv_packets:,}</b><span>KLV packets</span></div>
<div class="card"><b>{len(report.errors):,}</b><span>errors</span></div>
<div class="card"><b>{len(report.warnings):,}</b><span>warnings</span></div>
<div class="card"><b>{len(report.passes):,}</b><span>checks passed</span></div>
</section>
<h2>Elementary streams</h2>
<div class="panel table-wrap"><table><thead><tr><th>Program</th><th>PID</th><th>Kind</th>
<th>Type</th><th>Carriage</th><th>Codec</th><th>TS packets</th><th>PES</th><th>Bytes</th>
</tr></thead><tbody>{stream_rows}</tbody></table></div>
<h2>ST 0601 field coverage</h2>
{''.join(metadata_sections)}
<h2>Checks</h2>
<div class="panel table-wrap"><table><thead><tr><th>Status</th><th>Code</th><th>Message</th>
<th>Context</th><th>Count</th></tr></thead><tbody>{finding_rows}</tbody></table></div>
<footer>Generated by stanag4609 · A passing result covers the checks implemented by this library,
not independent certification of every applicable standard.</footer>
</main></body></html>
"""


def _render_finding(finding: VerificationFinding) -> str:
    context = _finding_context(finding)
    status_label = escape(finding.status.value.replace("_", " "))
    return (
        "<tr>"
        f'<td><span class="status {finding.status.value}">{status_label}</span></td>'
        f"<td><code>{escape(finding.code)}</code></td>"
        f'<td class="message">{escape(finding.message)}</td>'
        f'<td class="context">{escape(context) if context else "—"}</td>'
        f"<td>{finding.count:,}</td>"
        "</tr>"
    )

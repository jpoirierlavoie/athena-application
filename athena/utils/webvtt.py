"""Pure Microsoft Teams WebVTT transcript & Copilot aiInsights formatter.

Pure Python stdlib — zero Flask, zero Firestore, zero third-party parsers.
All string scans are linear-time (no nested quantifiers, CWE-1333 compliant).
"""

from __future__ import annotations

from dataclasses import dataclass
import html
from typing import Any, Iterable

_DEFAULT_COALESCE_GAP_SECONDS = 5.0
_DEFAULT_COALESCE_MAX_CHARS = 1200


@dataclass(frozen=True)
class TranscriptCue:
    """Single (optionally coalesced) spoken turn in a Teams transcript."""

    start_hms: str
    end_hms: str
    start_seconds: float
    end_seconds: float
    speaker: str
    text: str


def _parse_timestamp_seconds(raw: str) -> float | None:
    """Parse ``HH:MM:SS.mmm`` or ``MM:SS.mmm`` into seconds, or ``None``."""
    cleaned = (raw or "").strip().replace(",", ".")
    if not cleaned:
        return None
    parts = cleaned.split(":")
    if len(parts) == 2:
        hours_str = "0"
        mins_str, secs_str = parts
    elif len(parts) == 3:
        hours_str, mins_str, secs_str = parts
    else:
        return None
    try:
        hours = int(hours_str)
        mins = int(mins_str)
        secs = float(secs_str)
    except ValueError:
        return None
    if hours < 0 or not (0 <= mins < 60) or not (0.0 <= secs < 60.0):
        return None
    return hours * 3600.0 + mins * 60.0 + secs


def _format_hms(total_seconds: float) -> str:
    whole = max(0, int(total_seconds))
    hours, rem = divmod(whole, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def _strip_tags_linear(text: str) -> str:
    """Remove ``<...>`` markup tags in a single O(n) pass."""
    out: list[str] = []
    in_tag = False
    for ch in text:
        if ch == "<":
            in_tag = True
        elif ch == ">":
            if in_tag:
                in_tag = False
            else:
                out.append(ch)
        elif not in_tag:
            out.append(ch)
    return "".join(out)


def _extract_speaker_and_text(raw_body: str) -> tuple[str, str]:
    """Extract optional Teams ``<v Speaker>`` label and clean spoken text."""
    speaker = ""
    working = raw_body.strip()
    if working.startswith("<v "):
        close_idx = working.find(">")
        if close_idx > 3:
            raw_speaker = working[3:close_idx]
            speaker = " ".join(html.unescape(raw_speaker).split())
            working = working[close_idx + 1 :]
    elif working.startswith("<v."):
        # WebVTT class-annotated voice span: ``<v.class Speaker>text``
        close_idx = working.find(">")
        if close_idx > 3:
            space_idx = working.find(" ", 3, close_idx)
            if space_idx != -1:
                raw_speaker = working[space_idx + 1 : close_idx]
                speaker = " ".join(html.unescape(raw_speaker).split())
            working = working[close_idx + 1 :]

    stripped = _strip_tags_linear(working)
    clean_text = " ".join(html.unescape(stripped).split())
    return speaker, clean_text


def _parse_timing_line(line: str) -> tuple[float, float, str, str] | None:
    if " --> " not in line:
        return None
    left, right = line.split(" --> ", 1)
    # Strip optional cue settings appended after the end timestamp.
    right_tok = right.strip().split(None, 1)[0] if right.strip() else ""
    start_s = _parse_timestamp_seconds(left)
    end_s = _parse_timestamp_seconds(right_tok)
    if start_s is None or end_s is None:
        return None
    if end_s < start_s:
        end_s = start_s
    return start_s, end_s, _format_hms(start_s), _format_hms(end_s)


def parse_webvtt(
    raw: bytes | str,
    *,
    coalesce_gap_seconds: float = _DEFAULT_COALESCE_GAP_SECONDS,
    coalesce_max_chars: int = _DEFAULT_COALESCE_MAX_CHARS,
) -> list[TranscriptCue]:
    """Parse Microsoft Teams WebVTT content into coalesced :class:`TranscriptCue` rows."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    elif isinstance(raw, str):
        text = raw
    else:
        return []

    if text.startswith("\ufeff"):
        text = text[1:]
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    # Split into blank-line-delimited blocks without allocating regex backtracking.
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip() == "":
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)

    cues: list[TranscriptCue] = []
    for idx, block in enumerate(blocks):
        first = block[0].lstrip()
        if idx == 0 and first.startswith("WEBVTT"):
            # Even if the first block lacks a blank line before timing, check for timing inside it.
            timing_idx = next(
                (i for i, b in enumerate(block) if " --> " in b), None
            )
            if timing_idx is None:
                continue
            block = block[timing_idx:]
            first = block[0].lstrip()

        if (
            first.startswith("NOTE")
            or first.startswith("STYLE")
            or first.startswith("REGION")
        ):
            continue

        timing_idx = next(
            (i for i, b in enumerate(block) if _parse_timing_line(b) is not None),
            None,
        )
        if timing_idx is None:
            continue
        parsed_timing = _parse_timing_line(block[timing_idx])
        if parsed_timing is None:
            continue
        start_s, end_s, start_hms, end_hms = parsed_timing
        payload_lines = block[timing_idx + 1 :]
        if not payload_lines:
            continue

        speaker, spoken = _extract_speaker_and_text(" ".join(payload_lines))
        if not spoken:
            continue

        if (
            cues
            and speaker
            and cues[-1].speaker == speaker
            and (start_s - cues[-1].end_seconds) <= coalesce_gap_seconds
            and (len(cues[-1].text) + 1 + len(spoken)) <= coalesce_max_chars
        ):
            prev = cues[-1]
            cues[-1] = TranscriptCue(
                start_hms=prev.start_hms,
                end_hms=end_hms if end_s >= prev.end_seconds else prev.end_hms,
                start_seconds=prev.start_seconds,
                end_seconds=max(prev.end_seconds, end_s),
                speaker=prev.speaker,
                text=f"{prev.text} {spoken}",
            )
        else:
            cues.append(
                TranscriptCue(
                    start_hms=start_hms,
                    end_hms=end_hms,
                    start_seconds=start_s,
                    end_seconds=end_s,
                    speaker=speaker,
                    text=spoken,
                )
            )
    return cues


def extract_speakers(
    cues: Iterable[TranscriptCue], *, limit: int = 20
) -> list[str]:
    """Return distinct non-empty speakers in order of first appearance."""
    seen: list[str] = []
    seen_lower: set[str] = set()
    for cue in cues:
        name = (cue.speaker or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        seen.append(name)
        if len(seen) >= limit:
            break
    return seen


def format_transcript_preview(
    cues: Iterable[TranscriptCue], *, max_chars: int = 2000
) -> str:
    """Format a bounded plain-text preview for Réception / MCP list cards."""
    if max_chars <= 0:
        return ""
    lines: list[str] = []
    total = 0
    for cue in cues:
        line = (
            f"[{cue.start_hms}] {cue.speaker} : {cue.text}"
            if cue.speaker
            else f"[{cue.start_hms}] {cue.text}"
        )
        added_len = len(line) + (1 if lines else 0)
        if total + added_len > max_chars:
            if not lines:
                return line[: max(0, max_chars - 1)].rstrip() + "…"
            remaining = max_chars - total - 2
            if remaining > 20:
                lines.append(line[:remaining].rstrip() + "…")
            elif lines:
                lines[-1] = lines[-1][: max(0, len(lines[-1]) - 1)] + "…"
            break
        lines.append(line)
        total += added_len
    return "\n".join(lines)


def _safe_md_fragment(raw: Any) -> str:
    """Normalize Microsoft Graph text so sanitize/Markdown keeps all text safely."""
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    # Convert HTML chevrons from Graph strings into visual guillemets so
    # security.sanitize never drops an enclosed passage on save.
    text = text.replace("<", "‹").replace(">", "›")
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def format_copilot_insights_markdown(insight: dict[str, Any] | None) -> str:
    """Format a Graph ``callAiInsight`` payload into clean French Markdown."""
    if not isinstance(insight, dict):
        return ""

    sections: list[str] = []

    raw_notes = insight.get("meetingNotes")
    if isinstance(raw_notes, list) and raw_notes:
        note_blocks: list[str] = ["## Synthèse de la réunion"]
        any_note = False
        for item in raw_notes:
            if not isinstance(item, dict):
                continue
            title = _safe_md_fragment(item.get("title"))
            body = _safe_md_fragment(item.get("text"))
            raw_subs = item.get("subpoints")
            subs: list[str] = []
            if isinstance(raw_subs, list):
                for sub in raw_subs:
                    if not isinstance(sub, dict):
                        continue
                    sub_title = _safe_md_fragment(sub.get("title"))
                    sub_text = _safe_md_fragment(sub.get("text"))
                    if sub_title and sub_text:
                        subs.append(f"- **{sub_title}** : {sub_text}")
                    elif sub_title:
                        subs.append(f"- **{sub_title}**")
                    elif sub_text:
                        subs.append(f"- {sub_text}")
            if not (title or body or subs):
                continue
            any_note = True
            if title:
                note_blocks.append(f"### {title}")
            if body:
                note_blocks.append(body)
            if subs:
                note_blocks.append("\n".join(subs))
        if any_note:
            sections.append("\n\n".join(note_blocks))

    raw_actions = insight.get("actionItems")
    if isinstance(raw_actions, list) and raw_actions:
        action_lines: list[str] = []
        for item in raw_actions:
            if not isinstance(item, dict):
                continue
            owner = _safe_md_fragment(item.get("ownerDisplayName"))
            title = _safe_md_fragment(item.get("title"))
            text = _safe_md_fragment(item.get("text"))
            if not (owner or title or text):
                continue
            parts: list[str] = []
            if owner:
                parts.append(f"**{owner}**")
            if title:
                parts.append(f"**{title}**" if not owner else f"**{title}**")
            prefix = " — ".join(parts)
            if prefix and text:
                action_lines.append(f"- {prefix} : {text}")
            elif prefix:
                action_lines.append(f"- {prefix}")
            else:
                action_lines.append(f"- {text}")
        if action_lines:
            sections.append("## Suivis et actions\n\n" + "\n".join(action_lines))

    return "\n\n".join(sections).strip()


__all__ = [
    "TranscriptCue",
    "extract_speakers",
    "format_copilot_insights_markdown",
    "format_transcript_preview",
    "parse_webvtt",
]

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


BLOCK_RE = re.compile(
    r"(?ms)^\s*(\d+)\s*\n"
    r"(\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3})\s*\n"
    r"(.*?)(?=\n{2,}|\Z)"
)


@dataclass(frozen=True)
class BilingualBlock:
    number: int
    timing: str
    source: str
    target: str


def parse_bilingual_srt(
    text: str,
    *,
    top_line_role: str = "source",
) -> list[BilingualBlock]:
    if top_line_role not in {"source", "target"}:
        raise ValueError("top_line_role must be source or target")
    blocks: list[BilingualBlock] = []
    for match in BLOCK_RE.finditer(text.replace("\r\n", "\n")):
        lines = [line.strip() for line in match.group(3).splitlines() if line.strip()]
        if len(lines) < 2:
            raise ValueError(f"cue {match.group(1)} is not bilingual")
        top = lines[0]
        bottom = " ".join(lines[1:])
        source, target = (
            (top, bottom) if top_line_role == "source" else (bottom, top)
        )
        blocks.append(
            BilingualBlock(
                number=int(match.group(1)),
                timing=match.group(2).strip(),
                source=source,
                target=target,
            )
        )
    if not blocks:
        raise ValueError("no SRT cues found")
    return blocks


def render_track(
    blocks: list[BilingualBlock],
    *,
    mode: str,
    inline_separator: str = " ",
) -> str:
    if mode not in {"a", "b", "ab_two_line", "ab_inline"}:
        raise ValueError(f"unknown subtitle export mode: {mode}")
    rendered: list[str] = []
    for position, block in enumerate(blocks, start=1):
        if mode == "a":
            body = block.source
        elif mode == "b":
            body = block.target
        elif mode == "ab_two_line":
            body = f"{block.source}\n{block.target}"
        else:
            body = f"{block.source}{inline_separator}{block.target}"
        rendered.append(f"{position}\n{block.timing}\n{body}")
    return "\n\n".join(rendered) + "\n"


def export_four_modes(
    source_srt: Path,
    output_dir: Path,
    *,
    top_line_role: str = "source",
    inline_separator: str = " ",
    stem: str = "substar",
) -> list[Path]:
    blocks = parse_bilingual_srt(
        source_srt.read_text(encoding="utf-8-sig"),
        top_line_role=top_line_role,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    modes = {
        "a": f"{stem}_A.srt",
        "b": f"{stem}_B.srt",
        "ab_two_line": f"{stem}_AB_two_line.srt",
        "ab_inline": f"{stem}_AB_inline.srt",
    }
    outputs: list[Path] = []
    for mode, filename in modes.items():
        path = output_dir / filename
        path.write_text(
            render_track(
                blocks,
                mode=mode,
                inline_separator=inline_separator,
            ),
            encoding="utf-8-sig",
        )
        outputs.append(path)
    return outputs

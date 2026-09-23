"""ASS rendering shared by file export, frame previews and video burn-in."""
from __future__ import annotations

import re
import unicodedata
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from substar_core.domain import EntityState


class AssStyle(BaseModel):
    font: str = Field(default="Microsoft YaHei", min_length=1, max_length=100)
    size: int = Field(default=54, ge=8, le=300)
    color: str = "#FFFFFF"
    highlight: str = "#FFFF00"
    outline_color: str = "#000000"
    outline: float = Field(default=2, ge=0, le=15)
    shadow: float = Field(default=1, ge=0, le=15)
    bold: bool = False
    background: bool = False
    alignment: int = Field(default=2, ge=1, le=9)
    margin_x: int = Field(default=60, ge=0, le=2000)
    margin_y: int = Field(default=60, ge=0, le=2000)
    fade_ms: int = Field(default=0, ge=0, le=2000)

    @field_validator("color", "highlight", "outline_color")
    @classmethod
    def color_value(cls, value):
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
            raise ValueError("颜色应为 #RRGGBB")
        return value.upper()

    @field_validator("font")
    @classmethod
    def font_value(cls, value):
        if any(c in value for c in ",\r\n{}\\"):
            raise ValueError("字体名称包含无效字符")
        return value


class AssOptions(BaseModel):
    width: int = Field(default=1920, ge=16, le=8192)
    height: int = Field(default=1080, ge=16, le=8192)
    mode: str = "source"
    style: AssStyle = Field(default_factory=AssStyle)
    target_style: AssStyle | None = None
    word_highlight: bool = False

    @field_validator("mode")
    @classmethod
    def mode_value(cls, value):
        if value not in {"source", "target", "ab-double"}:
            raise ValueError("ASS 模式应为 source、target 或 ab-double")
        return value


def ass_color(value):
    return "&H00" + value[5:7] + value[3:5] + value[1:3]


def escape_text(value):
    # libass supports escaped braces, but not an escaped literal backslash.
    # A zero-width word joiner prevents literal \N/\n/\h becoming controls.
    return "".join({"\\": "\\\u2060", "{": r"\{", "}": r"\}",
                    "\r": "", "\n": r"\N", "\x00": ""}.get(c, c) for c in value)



def wrap_ass_text(text, style, width):
    """Provide explicit breaks even on libass builds without Unicode wrapping.

    Use a conservative em budget; native wrapping can still break earlier.
    ASS control tags do not consume width or interrupt word highlighting.
    """
    available = max(style.size, width - 2 * style.margin_x - 2 * (style.outline + style.shadow + 6))
    budget = available / style.size
    tokens = re.findall(r"\{[^{}]*\}|\\[Nnh]|.", text)
    lines, line, used, space = [], [], 0.0, None
    def weight(token):
        if token.startswith('{') or token == '\u2060' or unicodedata.combining(token[0]):
            return 0.0
        return 0.65 if token.isascii() and token not in 'WM@' else 1.0
    for token in tokens:
        if token in (r'\N', r'\n'):
            lines.append(''.join(line)); line=[]; used=0; space=None
            continue
        size = weight(token)
        if used + size > budget and used:
            if space is not None:
                lines.append(''.join(line[:space]))
                line=line[space+1:]
                used=sum(weight(t) for t in line)
            else:
                lines.append(''.join(line)); line=[]; used=0
            space=None
        if token == ' ':
            space=len(line)
        line.append(token); used += size
    lines.append(''.join(line))
    return r'\N'.join(lines)

def stamp(seconds):
    total = max(0, round(seconds * 100))
    minutes, remainder = divmod(total, 6000)
    hours, minutes = divmod(minutes, 60)
    sec, centi = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{sec:02d}.{centi:02d}"


def render_ass(document, options: AssOptions | None = None, *, cue_ids=None):
    options = options or AssOptions()
    style = options.style
    target_style = options.target_style or style
    if options.mode == "ab-double" and options.target_style is None:
        from substar_core.domain import DisplayOrder
        if document.presentation.display_order is DisplayOrder.SOURCE_ABOVE_TARGET:
            style = style.model_copy(update={"margin_y":style.margin_y + target_style.size + 12})
        else:
            target_style = target_style.model_copy(update={"margin_y":style.margin_y + style.size + 12})
    output = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {options.width}",
        f"PlayResY: {options.height}", "ScaledBorderAndShadow: yes", "WrapStyle: 0", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"]
    for name, item in (("Source", style), ("Target", target_style)):
        output.append(f"Style: {name},{item.font},{item.size},{ass_color(item.color)},{ass_color(item.highlight)},"
            f"{ass_color(item.outline_color)},&H80000000,{-1 if item.bold else 0},0,0,0,100,100,0,0,1,"
            f"{item.outline},{item.shadow},{item.alignment},{item.margin_x},{item.margin_x},{item.margin_y},1")
        if item.background:
            output.append(f"Style: {name}Box,{item.font},{item.size},&HFF000000,&HFF000000,"
                f"&H80000000,&H80000000,{-1 if item.bold else 0},0,0,0,100,100,0,0,3,"
                f"6,0,{item.alignment},{item.margin_x},{item.margin_x},{item.margin_y},1")
    output += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    source_by_id = {t.token_id:t for t in document.source_tokens}
    display = {t.token_id:t for t in document.display_tokens}
    from substar_core.language_layout import layout_tokens
    from substar_core.presentation import project_cue_lines
    from substar_core.chinese_script import convert_chinese_script

    def event(start, end, name, text, raw=False):
        if round(end * 100) <= round(start * 100) or not text:
            return
        chosen = style if name == "Source" else target_style
        encoded = wrap_ass_text(text if raw else escape_text(text), chosen, options.width)
        fade = min(chosen.fade_ms, round((end-start)*500))
        effect = f"{{\\fad({fade},{fade})}}" if fade and not raw else ""
        if chosen.background:
            plain = re.sub(r"\{\\c&H[0-9A-Fa-f]+&\}", "", encoded) if raw else encoded
            output.append(f"Dialogue: 0,{stamp(start)},{stamp(end)},{name}Box,,0,0,0,,{effect}{plain}")
        output.append(f"Dialogue: {1 if chosen.background else 0},{stamp(start)},{stamp(end)},{name},,0,0,0,,{effect}{encoded}")

    for cue in sorted(document.cues, key=lambda c:c.index):
        if cue.state is not EntityState.ACTIVE:
            continue
        if cue_ids is not None and cue.cue_id not in cue_ids:
            continue
        tokens = [display[t] for t in cue.display_token_ids if display[t].state is EntityState.ACTIVE]
        source = cue.source_text if cue.source_text is not None else layout_tokens(t.text for t in tokens)
        target = cue.target.target_text if cue.target else ""
        source, target = project_cue_lines(document, source=source, target=target)
        if document.properties.script_projection != "original":
            source = convert_chinese_script(source, document.properties.script_projection)
            target = convert_chinese_script(target, document.properties.script_projection)
        if options.mode in {"source", "ab-double"} and source:
            # Highlight only the currently spoken token, retaining full line context.
            raw_source = layout_tokens(t.text for t in tokens)
            if options.word_highlight and tokens and cue.source_text is None:
                spans, cursor = [], 0
                for token in tokens:
                    shown, _ = project_cue_lines(document, source=token.text, target="")
                    if document.properties.script_projection != "original":
                        shown = convert_chinese_script(shown, document.properties.script_projection)
                    shown = shown.strip()
                    if not shown:
                        continue
                    begin = source.find(shown, cursor)
                    if begin < 0:
                        raise ValueError("字幕显示文字无法映射到词元，不能生成逐词高亮")
                    bounds = [source_by_id[t] for t in token.source_token_ids]
                    if not bounds:
                        raise ValueError("词元缺少时间，不能生成逐词高亮")
                    if bounds:
                        spans.append((max(cue.start,min(t.start for t in bounds)),
                                      min(cue.end,max(t.end for t in bounds)), begin, begin+len(shown)))
                    cursor = begin+len(shown)
                edges = sorted({cue.start,cue.end,*[v for a,b,_,_ in spans if b>a for v in (a,b)]})
                for a,b in zip(edges,edges[1:]):
                    active = next((s for s in spans if s[0] <= a < s[1]), None)
                    text = escape_text(source)
                    if active:
                        left,right = active[2:]
                        text = escape_text(source[:left])+f"{{\\c{ass_color(style.highlight)}&}}"+escape_text(source[left:right])+f"{{\\c{ass_color(style.color)}&}}"+escape_text(source[right:])
                    event(a,b,"Source",text,True)
            elif options.word_highlight:
                raise ValueError("逐词高亮需要可映射的词级时间和显示文字")
            else:
                event(cue.start,cue.end,"Source",source)
        if options.mode in {"target", "ab-double"}:
            event(cue.start,cue.end,"Target",target)
    return "\n".join(output)+"\n"


def render_media(media: Path, ass: str, directory: Path, *, preview_seconds: float | None = None,
                 ffmpeg: str | None = None):
    from substar_core.config import INSTALL_ROOT
    bundled = INSTALL_ROOT / "runtime/ffmpeg/bin/ffmpeg.exe"
    executable = ffmpeg or (str(bundled) if bundled.is_file() else shutil.which("ffmpeg"))
    if not executable:
        raise ValueError("未找到 FFmpeg")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "captions.ass").write_text(ass, encoding="utf-8-sig")
    output = directory / ("preview.png" if preview_seconds is not None else "subtitled.mp4")
    if output.resolve() == media.resolve():
        raise ValueError("输出不能覆盖原媒体")
    args = [executable,"-nostdin","-hide_banner","-loglevel","error","-y","-i",str(media.resolve()),
            "-vf","ass=filename=captions.ass"]
    if preview_seconds is not None:
        args += ["-ss",str(max(0,preview_seconds)),"-frames:v","1"]
    else:
        args += ["-map","0:v:0","-map","0:a?","-c:v","libx264","-crf","18","-preset","medium","-c:a","aac","-movflags","+faststart"]
    args.append(str(output.resolve()))
    subprocess.run(args, cwd=directory, check=True, capture_output=True, timeout=86400,
                   creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    return output

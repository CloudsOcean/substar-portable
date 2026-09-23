"""Named, reusable layer profiles, carried by existing revision metadata.

Architecture reference: Moy's styles / profiles / assignments separation.
This implementation is original; no Moy source is copied.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from substar_core.ass_subtitles import AssStyle, AssOptions, render_ass
from substar_core.domain import ChangeKind, ChangeProvenance


class Layer(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=80)
    content: Literal["source", "target"]
    style_id: str
    enabled: bool = True
    word_highlight: bool = False


class Profile(BaseModel):
    name: str = Field(default="双语字幕", min_length=1, max_length=80)
    styles: dict[str, AssStyle] = Field(min_length=1, max_length=32)
    layers: list[Layer] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def references(self):
        if len({layer.id for layer in self.layers}) != len(self.layers):
            raise ValueError("字幕层 ID 重复")
        if any(layer.style_id not in self.styles for layer in self.layers):
            raise ValueError("字幕层引用的样式不存在")
        if any(layer.word_highlight and layer.content != "source" for layer in self.layers):
            raise ValueError("译文没有词级时间，不能启用逐词高亮")
        return self


class Configuration(BaseModel):
    profiles: dict[str, Profile] = Field(max_length=2048)
    default_profile: str
    cue_profiles: dict[str, str] = Field(default_factory=dict)
    cue_layers: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def references(self):
        if self.default_profile not in self.profiles or any(v not in self.profiles for v in self.cue_profiles.values()):
            raise ValueError("字幕样式方案不存在")
        return self


def default_profile(document=None):
    reverse = document and document.presentation.display_order.value == "target_above_source"
    return Profile(styles={
        "upper": AssStyle(size=72, bold=True, margin_y=212, color="#FFFFFF", outline=3, shadow=3, background=True),
        "lower": AssStyle(size=64, bold=True, margin_y=119, color="#FFE083", outline=3, shadow=3, background=True),
    }, layers=[
        Layer(id="upper", name="上行", content="target" if reverse else "source", style_id="upper"),
        Layer(id="lower", name="下行", content="source" if reverse else "target", style_id="lower"),
    ])


def configuration(document):
    for change in reversed(document.changes):
        if change.operation == "ass_configuration" and "ass" in change.metadata:
            return Configuration.model_validate(change.metadata["ass"])
    return Configuration(profiles={"default": default_profile(document)}, default_profile="default")


def profile_id(profile):
    return hashlib.sha256(json.dumps(profile.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def effective_profile(config, cue_id):
    default = config.profiles[config.default_profile]
    if cue_id not in config.cue_profiles:
        return default
    override = config.profiles[config.cue_profiles[cue_id]]
    if cue_id not in config.cue_layers:
        return override  # A whole-profile assignment includes layer structure.
    result = default.model_copy(deep=True)
    for layer in override.layers:
        if layer.id not in config.cue_layers[cue_id]:
            continue
        key = "override_"+layer.id
        result.styles[key] = override.styles[layer.style_id]
        copy = layer.model_copy(update={"style_id": key})
        if any(x.id == layer.id for x in result.layers):
            result.layers = [copy if x.id == layer.id else x for x in result.layers]
        else:
            result.layers.append(copy)
    used = {x.style_id for x in result.layers}
    result.styles = {k:v for k,v in result.styles.items() if k in used}
    return result


def resolved_configuration(document):
    config = configuration(document)
    resolved = Configuration(profiles={config.default_profile:config.profiles[config.default_profile]},
        default_profile=config.default_profile)
    for cue_id in config.cue_profiles:
        profile = effective_profile(config, cue_id)
        key = profile_id(profile)
        resolved.profiles[key] = profile
        resolved.cue_profiles[cue_id] = key
    return resolved


def apply_profile(document, profile, cue_ids=None, reset=False, layer_id=None):
    config = configuration(document).model_copy(deep=True)
    if cue_ids is not None:
        if not cue_ids or not set(cue_ids) <= {c.cue_id for c in document.cues if c.state.value == "active"}:
            raise ValueError("请选择有效的字幕")
    if reset:
        if cue_ids is None:
            raise ValueError("恢复继承需要选择字幕")
        for cue_id in cue_ids:
            config.cue_profiles.pop(cue_id, None)
            config.cue_layers.pop(cue_id, None)
    else:
        if profile is None:
            raise ValueError("缺少样式方案")
        def merge(base):
            if not layer_id:
                return profile
            result = base.model_copy(deep=True)
            layer = next((x for x in profile.layers if x.id == layer_id), None)
            if layer is None:
                raise ValueError("字幕层不存在")
            style = profile.styles[layer.style_id]
            style_key = "layer_" + layer.id
            result.styles[style_key] = style
            new_layer = layer.model_copy(update={"style_id": style_key})
            result.layers = [new_layer if x.id == layer_id else x for x in result.layers]
            if not any(x.id == layer_id for x in base.layers):
                result.layers.append(new_layer)
            used = {x.style_id for x in result.layers}
            result.styles = {k:v for k,v in result.styles.items() if k in used}
            return Profile.model_validate(result.model_dump())
        for cue_id in cue_ids if cue_ids is not None else [None]:
            base = effective_profile(config, cue_id)
            effective = merge(base)
            key = profile_id(effective)
            config.profiles[key] = effective
            if cue_id is None:
                config.default_profile = key
            else:
                if layer_id and (cue_id not in config.cue_profiles or cue_id in config.cue_layers):
                    config.cue_layers[cue_id] = sorted({*config.cue_layers.get(cue_id, []), layer_id})
                elif not layer_id:
                    config.cue_layers.pop(cue_id, None)
                config.cue_profiles[cue_id] = key
    valid = {c.cue_id for c in document.cues}
    config.cue_profiles = {k:v for k,v in config.cue_profiles.items() if k in valid}
    config.cue_layers = {k:v for k,v in config.cue_layers.items() if k in config.cue_profiles}
    used = {config.default_profile, *config.cue_profiles.values()}
    config.profiles = {k:v for k,v in config.profiles.items() if k in used}
    change = ChangeProvenance(kind=ChangeKind.MANUAL, operation="ass_configuration", actor="editor",
        metadata={"ass":config.model_dump(), "cue_ids":cue_ids, "user_action":"字幕样式"})
    # Current state stores one configuration. Historical revisions retain theirs.
    return replace(document, changes=tuple(c for c in document.changes if c.operation != "ass_configuration")+(change,))


def render_configuration(document, width=1920, height=1080):
    config = resolved_configuration(document)
    grouped = {}
    for cue in document.cues:
        if cue.state.value == "active":
            grouped.setdefault(config.cue_profiles.get(cue.cue_id, config.default_profile), set()).add(cue.cue_id)
    styles, events, header = [], [], None
    for profile_index, (key, ids) in enumerate(grouped.items()):
        profile = config.profiles[key]
        for index, layer in enumerate(profile.layers):
            if not layer.enabled:
                continue
            # All styles use a 1080-high reference; preserve the source aspect ratio.
            result = render_ass(document, AssOptions(width=max(16,min(8192,round(width/height*1080))),
                height=1080, mode=layer.content, style=profile.styles[layer.style_id],
                word_highlight=layer.word_highlight), cue_ids=ids)
            prefix = f"P{profile_index}L{index}"
            before, after = result.split("[V4+ Styles]\n", 1)
            style_section, event_section = after.split("[Events]\n", 1)
            header = before
            for line in style_section.splitlines():
                if line.startswith("Style: "):
                    styles.append(line.replace("Style: ", "Style: "+prefix, 1))
            for line in event_section.splitlines():
                if line.startswith("Dialogue: "):
                    parts = line.split(",", 9)
                    parts[0] = f"Dialogue: {index*2 + (1 if parts[3] in {'Source','Target'} else 0)}"
                    parts[3] = prefix+parts[3]
                    events.append(",".join(parts))
    if header is None:
        return render_ass(document, AssOptions(width=width, height=height), cue_ids=set())
    template = render_ass(document, AssOptions(), cue_ids=set())
    style_format = next(x for x in template.splitlines() if x.startswith("Format: Name"))
    event_format = next(x for x in template.splitlines() if x.startswith("Format: Layer"))
    return header+"[V4+ Styles]\n"+style_format+"\n"+"\n".join(styles)+"\n\n[Events]\n"+event_format+"\n"+"\n".join(events)+"\n"

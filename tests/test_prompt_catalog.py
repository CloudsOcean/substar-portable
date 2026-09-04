from __future__ import annotations

import pytest

from substar_core.prompt_registry import (
    PromptRegistryError,
    prompt_catalog,
    prompt_component,
    update_prompt_component,
)


def test_prompt_catalog_projects_only_registered_production_components() -> None:
    catalog = prompt_catalog()
    assert catalog["schema_version"] == "substar.prompt-catalog.v1"
    assert catalog["routes_read_only"] is True
    assert catalog["components_editable"] is True
    assert catalog["stats"] == {
        "families": 10,
        "variants": 60,
        "components": 50,
        "core_components": 32,
        "cases": 18,
    }
    assert [item["id"] for item in catalog["categories"]] == [
        "segmentation", "translation", "editor", "exchange",
    ]
    paths = {item["path"] for item in catalog["components"]}
    assert "exchange/external_ai_prooftranslation.md" in paths
    assert "exchange/external_ai_split.md" in paths
    assert "exchange/external_ai_edit.md" in paths
    assert "experimental/merged_split.md" not in paths


def test_prompt_component_reads_registered_content_and_rejects_hidden_assets() -> None:
    component = prompt_component("translation/common/contextual_translation.md")
    assert component["schema_version"] == "substar.prompt-component.v1"
    assert component["kind"] == "template"
    assert "N:1" in component["text"]
    assert len(component["sha256"]) == 64

    one_to_one = prompt_component("translation/mode/one_to_one.md")
    assert "one_to_one" in one_to_one["text"]

    with pytest.raises(PromptRegistryError, match="未注册"):
        prompt_component("experimental/merged_split.md")
    with pytest.raises(PromptRegistryError, match="未注册"):
        prompt_component("../../app.py")


def test_prompt_component_update_is_atomic_and_rejects_stale_writes(
    tmp_path, monkeypatch
) -> None:
    production = tmp_path / "production"
    component_path = production / "common" / "prompt.md"
    component_path.parent.mkdir(parents=True)
    component_path.write_text("# Original\n", encoding="utf-8")
    (production / "registry.json").write_text(
        '{"schema_version":"substar.prompt-registry.v1","categories":[],"prompts":'
        '{"example":{"variants":{"default":["common/prompt.md"]}}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr("substar_core.prompt_registry.PROMPT_ROOT", tmp_path)
    monkeypatch.setattr(
        "substar_core.prompt_registry.REGISTRY_PATH", production / "registry.json"
    )

    current = prompt_component("common/prompt.md")
    updated = update_prompt_component(
        "common/prompt.md", "# Updated\n", expected_sha256=current["sha256"]
    )
    assert updated["text"] == "# Updated\n"
    assert component_path.read_text(encoding="utf-8") == "# Updated\n"

    with pytest.raises(PromptRegistryError, match="其他位置修改"):
        update_prompt_component(
            "common/prompt.md", "# Stale\n", expected_sha256=current["sha256"]
        )

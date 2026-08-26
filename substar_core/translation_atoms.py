from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


T1_ATOM_SCHEMA = "substar.translation-atoms.v3"
T2_ASSEMBLY_SCHEMA = "substar.translation-assembly.v3"


class TranslationAtomError(ValueError):
    """Raised when a T1/T2 response violates the V3 translation contract."""


@dataclass(frozen=True)
class TranslationAtom:
    atom_id: str
    text: str
    source_cue_ids: tuple[str, ...]


@dataclass(frozen=True)
class CueAssembly:
    cue_id: str
    atom_ids: tuple[str, ...]


@dataclass(frozen=True)
class AtomReuse:
    reuse_id: str
    atom_ids: tuple[str, ...]
    cue_ids: tuple[str, ...]


def _identifier(value: Any, *, field: str) -> str:
    identifier = str(value or "").strip()
    if not identifier:
        raise TranslationAtomError(f"{field} 不能为空")
    return identifier


def _expected_group_map(
    expected_groups: Iterable[dict[str, Any]],
) -> tuple[list[str], dict[str, list[str]]]:
    order: list[str] = []
    cue_ids: dict[str, list[str]] = {}
    for raw in expected_groups:
        group_id = _identifier(raw.get("group_id"), field="group_id")
        if group_id in cue_ids:
            raise TranslationAtomError(f"重复的意义组 ID：{group_id}")
        raw_cues = raw.get("cue_ids")
        if raw_cues is None:
            raw_cues = [cue.get("cue_id") for cue in raw.get("cues", [])]
        if not isinstance(raw_cues, list) or not raw_cues:
            raise TranslationAtomError(f"{group_id} 没有固定 Cue 槽位")
        normalized = [_identifier(value, field="cue_id") for value in raw_cues]
        if len(normalized) != len(set(normalized)):
            raise TranslationAtomError(f"{group_id} 包含重复 Cue ID")
        order.append(group_id)
        cue_ids[group_id] = normalized
    if not order:
        raise TranslationAtomError("翻译输入没有意义组")
    return order, cue_ids


def validate_t1_atoms(
    result: dict[str, Any],
    expected_groups: Iterable[dict[str, Any]],
) -> dict[str, list[TranslationAtom]]:
    """Validate T1's authoritative target-language semantic atoms.

    Source cue references establish provenance but are not a partition: a single
    source cue can legitimately contribute to multiple target-language atoms.
    T2 may deliberately copy an atom into several independent Cue texts, but
    that reuse must be declared explicitly in the assembly contract.
    """

    if result.get("schema_version") != T1_ATOM_SCHEMA:
        raise TranslationAtomError("T1 schema_version 错误")
    expected_order, expected_cues = _expected_group_map(expected_groups)
    raw_groups = result.get("groups")
    if not isinstance(raw_groups, list):
        raise TranslationAtomError("T1 结果缺少 groups")
    actual_order = [str(item.get("group_id", "")) for item in raw_groups]
    if actual_order != expected_order:
        raise TranslationAtomError("T1 group_id 未完整按序覆盖输入")

    all_atom_ids: set[str] = set()
    validated: dict[str, list[TranslationAtom]] = {}
    for raw_group in raw_groups:
        group_id = str(raw_group["group_id"])
        if not str(raw_group.get("group_translation", "")).strip():
            raise TranslationAtomError(f"{group_id} 缺少完整自然译文 group_translation")
        raw_atoms = raw_group.get("atoms")
        if not isinstance(raw_atoms, list) or not raw_atoms:
            raise TranslationAtomError(f"{group_id} 没有语义原子")
        atoms: list[TranslationAtom] = []
        covered_source_cues: set[str] = set()
        valid_cues = expected_cues[group_id]
        for raw_atom in raw_atoms:
            if not isinstance(raw_atom, dict):
                raise TranslationAtomError(f"{group_id} 的原子必须是对象")
            atom_id = _identifier(raw_atom.get("atom_id"), field="atom_id")
            if atom_id in all_atom_ids:
                raise TranslationAtomError(f"重复的 atom_id：{atom_id}")
            text = str(raw_atom.get("text", "")).strip()
            if not text:
                raise TranslationAtomError(f"{atom_id} 译文为空")
            raw_sources = raw_atom.get("source_cue_ids")
            if not isinstance(raw_sources, list) or not raw_sources:
                raise TranslationAtomError(f"{atom_id} 缺少 source_cue_ids")
            sources = tuple(_identifier(value, field="source_cue_id") for value in raw_sources)
            if len(sources) != len(set(sources)):
                raise TranslationAtomError(f"{atom_id} source_cue_ids 重复")
            if any(value not in valid_cues for value in sources):
                raise TranslationAtomError(f"{atom_id} 引用了组外 Cue")
            if list(sources) != sorted(sources, key=valid_cues.index):
                raise TranslationAtomError(f"{atom_id} 内部源 Cue 顺序错误")
            all_atom_ids.add(atom_id)
            covered_source_cues.update(sources)
            atoms.append(TranslationAtom(atom_id, text, sources))
        if covered_source_cues != set(valid_cues):
            missing = sorted(set(valid_cues) - covered_source_cues, key=valid_cues.index)
            raise TranslationAtomError(f"{group_id} 原子未覆盖源 Cue：{missing}")
        validated[group_id] = atoms
    return validated


def validate_t2_assembly(
    result: dict[str, Any],
    expected_groups: Iterable[dict[str, Any]],
    atoms_by_group: dict[str, list[TranslationAtom]],
) -> dict[str, list[CueAssembly]]:
    """Validate deliberate atom allocation and copy-on-materialize reuse."""

    if result.get("schema_version") != T2_ASSEMBLY_SCHEMA:
        raise TranslationAtomError("T2 schema_version 错误")
    expected_order, expected_cues = _expected_group_map(expected_groups)
    raw_groups = result.get("groups")
    if not isinstance(raw_groups, list):
        raise TranslationAtomError("T2 结果缺少 groups")
    actual_order = [str(item.get("group_id", "")) for item in raw_groups]
    if actual_order != expected_order:
        raise TranslationAtomError("T2 group_id 未完整按序覆盖输入")

    validated: dict[str, list[CueAssembly]] = {}
    for raw_group in raw_groups:
        group_id = str(raw_group["group_id"])
        expected_atom_ids = [atom.atom_id for atom in atoms_by_group[group_id]]
        raw_assignments = raw_group.get("assignments")
        if not isinstance(raw_assignments, list):
            raise TranslationAtomError(f"{group_id} 缺少 assignments")
        actual_cues = [str(item.get("cue_id", "")) for item in raw_assignments]
        if actual_cues != expected_cues[group_id]:
            raise TranslationAtomError(f"{group_id} 必须完整按序返回固定 Cue 槽位")

        consumed: list[str] = []
        atom_to_cues: dict[str, list[str]] = {atom_id: [] for atom_id in expected_atom_ids}
        assignments: list[CueAssembly] = []
        for raw in raw_assignments:
            cue_id = str(raw["cue_id"])
            raw_atom_ids = raw.get("atom_ids")
            if not isinstance(raw_atom_ids, list):
                raise TranslationAtomError(f"{cue_id} atom_ids 必须是数组")
            atom_ids = tuple(_identifier(value, field="atom_id") for value in raw_atom_ids)
            if not atom_ids:
                raise TranslationAtomError(f"{cue_id} 至少需要一个 T1 原子")
            if "surface_text" in raw or "text" in raw:
                raise TranslationAtomError(
                    f"{cue_id} T2 不得返回文本，只能分配 T1 atom_id"
                )
            if len(atom_ids) != len(set(atom_ids)):
                raise TranslationAtomError(f"{cue_id} 内部 atom_id 重复")
            unknown = [atom_id for atom_id in atom_ids if atom_id not in atom_to_cues]
            if unknown:
                raise TranslationAtomError(f"{cue_id} 引用了未知 T1 原子：{unknown}")
            if list(atom_ids) != sorted(atom_ids, key=expected_atom_ids.index):
                raise TranslationAtomError(f"{cue_id} 未保持 T1 目标语原子顺序")
            consumed.extend(atom_ids)
            for atom_id in atom_ids:
                atom_to_cues[atom_id].append(cue_id)
            assignments.append(CueAssembly(cue_id, atom_ids))
        missing = [atom_id for atom_id in expected_atom_ids if not atom_to_cues[atom_id]]
        if missing:
            raise TranslationAtomError(
                f"{group_id} 存在未使用的 T1 原子：{missing}"
            )
        raw_reuse_groups = raw_group.get("reuse_groups", [])
        if not isinstance(raw_reuse_groups, list):
            raise TranslationAtomError(f"{group_id} reuse_groups 必须是数组")
        declared_reuse: dict[str, tuple[str, ...]] = {}
        reuse_ids: set[str] = set()
        for raw_reuse in raw_reuse_groups:
            if not isinstance(raw_reuse, dict):
                raise TranslationAtomError(f"{group_id} reuse_groups 包含非对象项")
            reuse_id = _identifier(raw_reuse.get("reuse_id"), field="reuse_id")
            if reuse_id in reuse_ids:
                raise TranslationAtomError(f"{group_id} reuse_id 重复：{reuse_id}")
            reuse_ids.add(reuse_id)
            if raw_reuse.get("mode") != "copy":
                raise TranslationAtomError(f"{reuse_id} mode 必须为 copy")
            reuse_atoms = tuple(
                _identifier(value, field="reuse atom_id")
                for value in raw_reuse.get("atom_ids", [])
            )
            reuse_cues = tuple(
                _identifier(value, field="reuse cue_id")
                for value in raw_reuse.get("cue_ids", [])
            )
            if not reuse_atoms or len(reuse_cues) < 2:
                raise TranslationAtomError(f"{reuse_id} 必须声明原子和至少两个 Cue")
            unknown_reuse_cues = [
                cue_id for cue_id in reuse_cues if cue_id not in expected_cues[group_id]
            ]
            if unknown_reuse_cues:
                raise TranslationAtomError(
                    f"{reuse_id} 引用了组外 Cue：{unknown_reuse_cues}"
                )
            if list(reuse_cues) != sorted(reuse_cues, key=expected_cues[group_id].index):
                raise TranslationAtomError(f"{reuse_id} cue_ids 顺序错误")
            for atom_id in reuse_atoms:
                if atom_id not in atom_to_cues:
                    raise TranslationAtomError(f"{reuse_id} 引用了未知原子：{atom_id}")
                if tuple(atom_to_cues[atom_id]) != reuse_cues:
                    raise TranslationAtomError(
                        f"{reuse_id} 与 {atom_id} 的实际重复分配不一致"
                    )
                if atom_id in declared_reuse:
                    raise TranslationAtomError(f"{atom_id} 被多个 reuse_groups 重复声明")
                declared_reuse[atom_id] = reuse_cues
        undeclared = [
            atom_id for atom_id, cue_ids in atom_to_cues.items()
            if len(cue_ids) > 1 and atom_id not in declared_reuse
        ]
        if undeclared:
            raise TranslationAtomError(
                f"{group_id} 重复使用原子但未在 reuse_groups 声明：{undeclared}"
            )
        validated[group_id] = assignments
    return validated


def _join_atoms(texts: list[str], target_language: str) -> str:
    separator = "" if target_language.lower().startswith("zh") else " "
    return separator.join(text.strip() for text in texts if text.strip()).strip()


def materialize_t2_targets(
    atoms_by_group: dict[str, list[TranslationAtom]],
    assemblies_by_group: dict[str, list[CueAssembly]],
    *,
    target_language: str,
) -> dict[str, str]:
    targets: dict[str, str] = {}
    for group_id, assignments in assemblies_by_group.items():
        atoms = {atom.atom_id: atom for atom in atoms_by_group[group_id]}
        for assignment in assignments:
            text = _join_atoms(
                [atoms[atom_id].text for atom_id in assignment.atom_ids],
                target_language,
            )
            targets[assignment.cue_id] = text
    return targets


def stage2_result_from_assembly(
    result: dict[str, Any],
    expected_groups: list[dict[str, Any]],
) -> dict[str, Any]:
    """Materialize independent per-Cue text from a strict V3 assembly.

    Reused atoms are copied into standalone target strings.  The editor never
    retains a live link between the resulting Cue texts.
    """

    t1_result = {
        "schema_version": T1_ATOM_SCHEMA,
        "groups": [
            {
                "group_id": str(group["group_id"]),
                "group_translation": str(group.get("approved_translation", "")),
                "atoms": list(group.get("approved_atoms", [])),
            }
            for group in expected_groups
        ],
    }
    atoms_by_group = validate_t1_atoms(t1_result, expected_groups)
    assemblies = validate_t2_assembly(result, expected_groups, atoms_by_group)
    output_groups: list[dict[str, Any]] = []
    for group in expected_groups:
        group_id = str(group["group_id"])
        cue_order = [str(cue["cue_id"]) for cue in group["cues"]]
        atom_map = {atom.atom_id: atom for atom in atoms_by_group[group_id]}
        target_language = str(group["cues"][0].get("target_language", "zh-CN"))
        targets_by_cue = materialize_t2_targets(
            {group_id: atoms_by_group[group_id]},
            {group_id: assemblies[group_id]},
            target_language=target_language,
        )
        target_rows: list[dict[str, Any]] = []
        exact_one_to_one = True
        source_flow: list[float] = []
        for assignment in assemblies[group_id]:
            source_ids: list[str] = []
            for atom_id in assignment.atom_ids:
                for source_id in atom_map[atom_id].source_cue_ids:
                    if source_id not in source_ids:
                        source_ids.append(source_id)
            source_ids.sort(key=cue_order.index)
            exact_one_to_one = exact_one_to_one and source_ids == [assignment.cue_id]
            source_flow.append(
                sum(cue_order.index(value) for value in source_ids) / len(source_ids)
            )
            target_rows.append(
                {
                    "cue_id": int(assignment.cue_id),
                    "source_cue_ids": [int(value) for value in source_ids],
                    "text": targets_by_cue[assignment.cue_id],
                    "atom_ids": list(assignment.atom_ids),
                    "status": "ok",
                }
            )
        inversions = sum(
            source_flow[left] > source_flow[right]
            for left in range(len(source_flow))
            for right in range(left + 1, len(source_flow))
        )
        comparable_pairs = len(source_flow) * (len(source_flow) - 1) // 2
        reordering = (
            "none" if inversions == 0
            else "full" if comparable_pairs and inversions == comparable_pairs
            else "partial"
        )
        output_groups.append(
            {
                "group_id": group_id,
                "relation": "1:1" if exact_one_to_one else "N:M",
                "mapping_audit": {
                    "reordering": reordering,
                    "source_flow": source_flow,
                    "inversion_count": inversions,
                    "copy_on_materialize": True,
                },
                "targets": target_rows,
            }
        )
    return {
        "schema_version": "substar.stage2.translation.v1",
        "groups": output_groups,
        "terminology": [],
        "assembly_schema_version": T2_ASSEMBLY_SCHEMA,
    }

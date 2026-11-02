# Substar production prompts

`registry.json` is the only production prompt manifest. Runtime code requests a
stage and language variant through `substar_core.prompt_registry`; it must not
open individual prompt or case files directly.

Only files under `cases/*.constructed.md` are injected as examples. Historical
company examples, experiments and retired P2/P3/T1/T2 assets live under
`prompts/archive/` and are never loaded by the production registry.

Translation uses common rules plus one target-language component for each
actual target in the request. The twelve default targets have dedicated files
under `translation/target/`; other languages use the generic language rules.
Source languages do not select language-pair prompts or examples. Legacy pair
variant names are accepted only as aliases for their target. Old translation
pair files and `approved_en_zh/` remain historical references and are not in the
active registry. New task snapshots use `2026-11-02.target-language.1`; existing
frozen task snapshots keep their original content.

Models emit numbered subtitle lines or shared ranges directly. Runtime request
batching and internal validation records are not model-authored mapping steps.
The task's punctuation policy is appended to both primary and repair requests;
word spacing and lexical/numeric symbols remain meaningful in every language.

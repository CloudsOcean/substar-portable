# Substar production prompts

`registry.json` is the only production prompt manifest. Runtime code requests a
stage and language variant through `substar_core.prompt_registry`; it must not
open individual prompt or case files directly.

Only files under `cases/*.constructed.md` are injected as examples. Historical
company examples, experiments and retired P2/P3/T1/T2 assets live under
`prompts/archive/` and are never loaded by the production registry.

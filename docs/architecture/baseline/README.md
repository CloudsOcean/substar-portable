# Pre-refactor baseline

This directory freezes the observable Substar baseline before the backend architecture refactor.

## Source baseline

- Branch: `codex/backend-architecture-refactor`
- Commit: `6e5b95d48f9ce5a5fffe702bdfb6735025823b99`
- Python: `3.10.16`
- Node.js: `24.14.0`
- FFmpeg: `7.1.1-essentials_build-www.gyan.dev`

## Automated checks

| Check | Result |
| --- | --- |
| `python -m unittest discover -s tests -p "test_*.py"` | 25 passed |
| `node --test tests/editor_v2_language.test.js tests/editor_v2_cue_list_view.test.js` | 4 passed |
| `python -m compileall -q app.py substar_core` | passed |

## HTTP smoke checks

The running local application returned HTTP 200 for:

- `/api/system`
- `/api/settings`
- `/api/jobs`
- `/api/v2/projects`
- `/api/glossary`
- `/api/runtime/identity`

## Reference projects

- `public_projects/20260813_130916_split_10c8ce`: complete media, ASR alignment, editor database, translation run, and bilingual SRT.
- `public_projects/20260814_130031_split_388345`: media, ASR alignment, editor database, prompt snapshot, and segmentation artifacts.

The reference project files are not duplicated here. They remain tracked in their existing locations so the baseline does not create a second source of truth.

## Frozen interfaces

- `openapi.json` is generated from the FastAPI application at the baseline commit.
- `ui/` contains screenshots of the current workbench pages.

The split, editor, glossary, and settings pages loaded without browser console errors. The editor screenshot records the project selected by the running application at capture time; it is a visual baseline rather than a portable project fixture.

These files are characterization artifacts. They describe current behavior; they do not declare that every historical endpoint or payload must survive unchanged.

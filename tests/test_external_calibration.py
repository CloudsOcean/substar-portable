import unittest
from types import SimpleNamespace
from test_ai_calibration_merge import _document
from substar_core.editor.calibration.exchange import export_calibration, inspect_calibration, SCHEMA
from substar_core.editor.calibration.service import calibration_prompts

class ExternalCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.revision = SimpleNamespace(revision_id='revision_1', document=_document())
    def payload(self, output):
        return dict(schema_version=SCHEMA,project_id='p',revision_id='revision_1',output=output)
    def test_export_uses_production_route_and_protocol(self):
        text=export_calibration('p',self.revision,{'language':'en'},[], 'Keep proper names')
        self.assertIn('Keep proper names',text)
        self.assertIn('revision_1',text)
        self.assertIn('1|OWN|u s america',text)
        self.assertIn('project_id',text)
    def test_import_applies_calibration_and_keeps_timing(self):
        document, provenance, result=inspect_calibration('p',self.revision,self.payload('1|u s America.'))
        self.assertGreater(result['replacement_count'],0)
        self.assertEqual(document.cues[0].start,self.revision.document.cues[0].start)
        self.assertEqual(document.cues[0].end,self.revision.document.cues[0].end)
        self.assertEqual(document.display_tokens[-1].text,'America.')
        self.assertEqual(document.display_tokens[-1].provenance.kind.value,'ai')
    def test_invalid_and_stale_results_are_atomic(self):
        for payload in [self.payload(''),self.payload('99|Unknown.'),{**self.payload('1|u s America.'),'revision_id':'old'},{**self.payload('1|u s America.'),'project_id':'other'}]:
            with self.assertRaises(ValueError): inspect_calibration('p',self.revision,payload)
        self.assertEqual(self.revision.document.display_tokens[-1].text,'america')

    def test_http_preview_never_saves_and_apply_uses_revision_guard(self):
        import asyncio, io, json
        from unittest.mock import patch, Mock
        from starlette.datastructures import UploadFile
        from substar_core.editor import http_api as api
        request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(task_service=SimpleNamespace(list_tasks=lambda **kwargs:[]))))
        store=SimpleNamespace(load_latest=lambda:self.revision)
        payload=self.payload('1|u s America.')
        def upload(): return UploadFile(io.BytesIO(json.dumps(payload).encode()),filename='calibration.json')
        with patch.object(api,'open_project_store',return_value=store),patch.object(api,'_save_document',return_value={'revision_id':'new'}) as save:
            preview=asyncio.run(api.import_external_calibration('p',request,upload(),False))
            self.assertGreater(preview['replacement_count'],0)
            save.assert_not_called()
            applied=asyncio.run(api.import_external_calibration('p',request,upload(),True))
            self.assertEqual(applied['revision']['revision_id'],'new')
            self.assertEqual(save.call_args.kwargs['expected_revision_id'],'revision_1')
            self.assertEqual(save.call_count,1)
    def test_language_route_is_same_as_internal_calibration(self):
        from substar_core.prompt_registry import render_prompt
        for language in ('en','zh','ja','ko'):
            text=export_calibration('p',self.revision,{'language':language},[])
            self.assertIn(render_prompt('calibration',variant=language).text,text)
    def test_active_ai_task_blocks_import(self):
        import asyncio, io
        from starlette.datastructures import UploadFile
        from fastapi import HTTPException
        from substar_core.editor.http_api import import_external_calibration
        request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(task_service=SimpleNamespace(list_tasks=lambda **kwargs:[{'task_type':'calibration'}]))))
        with self.assertRaises(HTTPException) as error:
            asyncio.run(import_external_calibration('p',request,UploadFile(io.BytesIO(b'{}')),True))
        self.assertEqual(error.exception.status_code,423)

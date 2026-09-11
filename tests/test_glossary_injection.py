import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from substar_core import glossary as g

class GlossaryInjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name,value in [("GLOSSARY_FILE",root / "glossary.json"),("APP_DATA_DIR",root)]:
            p=patch.object(g,name,value);p.start();self.addCleanup(p.stop)
        self.collections=[{"id":"global","kind":"global","injection_permissions":["translation"]},{"id":"show","name":"节目","kind":"project","injection_permissions":["asr","calibration"]}]
        g.save_glossary_library(self.collections,[{"source":"Global","target":"全局","glossary_id":"global"},{"source":"Nova","target":"新星","glossary_id":"show"}])

    def test_candidate_import_preserves_background_and_terminal_states(self):
        g.collect_candidates([{"text": "Ignored"}, {"text": "Background"}], "manual")
        initial = g.load_glossary_library()
        ignored = next(c for c in initial["candidates"] if c["source"] == "Ignored")
        g.review_candidates([ignored["id"]], "ignore", "global")
        imported = [{"id": ignored["id"], "source": "New", "target": "新增", "status": "pending", "glossary_id": "show"},
                    {"source": "Ignored", "status": "pending"}]
        saved = g.save_glossary_library(initial["collections"], initial["entries"], imported_candidates=imported)
        self.assertEqual(len(saved["candidates"]), 3)
        self.assertEqual(next(c for c in saved["candidates"] if c["source"] == "Ignored")["status"], "ignored")
        self.assertEqual(len({c["id"] for c in saved["candidates"]}), 3)
        repeated = g.save_glossary_library(initial["collections"], initial["entries"], imported_candidates=imported)
        self.assertEqual(saved, repeated)
        with self.assertRaises(ValueError):
            g.save_glossary_library(initial["collections"], initial["entries"], imported_candidates=[{"source":"Bad","status":"invalid"}])
        self.assertEqual(g.load_glossary_library(), saved)

    def test_permissions_survive_save_and_separate_stages(self):
        self.assertEqual([e["source"] for e in g.active_glossary(stage="asr")],["Nova"])
        self.assertEqual([e["source"] for e in g.active_glossary(stage="calibration")],["Nova"])
        self.assertEqual([e["source"] for e in g.active_glossary(stage="translation")],[])
        self.assertEqual(g.active_glossary(stage="asr",collection_ids=[]),[])
        with self.assertRaises(ValueError):g.active_glossary(stage="asr",collection_ids=["global"])

    def test_unassigned_entries_are_preserved_but_never_injected(self):
        library = g.load_glossary_library()
        unassigned = next(c for c in library["collections"] if c["id"] == "global")
        self.assertEqual(unassigned["injection_permissions"], [])
        self.assertTrue(any(e["source"] == "Global" for e in library["entries"]))
        for stage in ("asr", "calibration", "translation"):
            with self.assertRaises(ValueError):
                g.active_glossary(stage=stage, collection_ids=["global"])

    def test_legacy_permissions_default_off_without_losing_entries(self):
        g.save_glossary_library([{"id":"show","name":"节目","kind":"project"}],[{"source":"Nova","glossary_id":"show"}])
        self.assertEqual(len(g.load_glossary()),1)
        self.assertEqual(g.active_glossary(stage="asr"),[])

    def test_merge_preserves_manual_value_without_priority_or_translation(self):
        preview=g.injection_preview(["show"],[{"text":"NOVA","weight":5}])
        self.assertEqual(preview["hotwords"],[{"text":"NOVA","weight":5}])
        self.assertEqual(g.injection_preview(["show"],[])["hotwords"],[{"text":"Nova","weight":5}])
        self.assertNotIn("新星",str(preview["hotwords"]))

    def test_glossary_preview_excludes_temporary_and_deduplicates_collections(self):
        library = g.load_glossary_library()
        library["collections"].append({"id":"second","name":"另一个节目","kind":"project","injection_permissions":["asr"]})
        library["entries"].extend([{"source":"NOVA","glossary_id":"second","hotword_weight":1}, {"source":"Cyrus","glossary_id":"second","hotword_weight":2}])
        g.save_glossary_library(library["collections"], library["entries"])
        result = g.injection_preview(["show","second"], [{"text":"nova","weight":50}])
        self.assertEqual(result["glossary_hotwords"], [{"text":"Cyrus","weight":5}])
        self.assertEqual(result["hotwords"], [{"text":"nova","weight":50},{"text":"Cyrus","weight":5}])
        result = g.injection_preview(["show","second"], [])
        self.assertEqual(result["glossary_hotwords"], [{"text":"Nova","weight":5},{"text":"Cyrus","weight":5}])
        self.assertEqual(g.injection_preview([], [{"text":"nova","weight":4}])["glossary_hotwords"], [])

    def test_overflow_is_an_error_not_truncation(self):
        with self.assertRaises(ValueError):g.injection_preview(["show"],[{"text":f"term{i}","weight":4} for i in range(2000)])

    def test_candidates_dedup_review_and_ignore(self):
        a=g.collect_candidates([{"text":"Alpha","target":"阿尔法"}],"ai_asr","video")
        g.collect_candidates([{"text":"alpha"}],"manual","video")
        pool=g.load_glossary_library()["candidates"]
        self.assertEqual(len(pool),1);self.assertEqual(len(pool[0]["sources"]),2)
        self.assertEqual(len(g.active_glossary(stage="asr")),1)
        accepted=g.review_candidates([pool[0]["id"]],"approve","show")
        self.assertEqual(len(accepted["entries"]),3)
        self.assertEqual(g.active_glossary(stage="asr")[-1]["target"],"阿尔法")
        g.review_candidates([pool[0]["id"]],"approve","show")
        self.assertEqual(len(g.load_glossary()),3)
        b=g.collect_candidates([{"text":"Beta"}],"ai_direct")
        g.review_candidates([b["candidates"][-1]["id"]],"ignore","show")
        g.collect_candidates([{"text":"Beta"}],"manual")
        self.assertEqual(g.load_glossary_library()["candidates"][-1]["status"],"ignored")

    def test_normal_save_preserves_concurrently_collected_candidates(self):
        old=g.load_glossary_library()
        g.collect_candidates([{"text":"Alpha"}],"manual")
        g.save_glossary_library(old["collections"],old["entries"])
        self.assertEqual(len(g.load_glossary_library()["candidates"]),1)

    def test_api_round_trip(self):
        import httpx
        import app
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),base_url="http://test") as c:
                result=await c.post("/api/glossary/candidates",json={"rows":[{"text":"Alpha"}],"source":"external"})
                self.assertEqual(result.status_code,200)
                candidate=result.json()["candidates"][0]
                reviewed=await c.post("/api/glossary/candidates/review",json={"ids":[candidate["id"]],"action":"approve","glossary_id":"show"})
                self.assertEqual(reviewed.status_code,200)
                denied=await c.post("/api/glossary/injection-preview",json={"collection_ids":["global"]})
                self.assertEqual(denied.status_code,400)
                allowed=await c.post("/api/glossary/injection-preview",json={"collection_ids":["show"]})
                self.assertEqual([e["text"] for e in allowed.json()["hotwords"]],["Nova","Alpha"])
        asyncio.run(run())

    def test_task_request_uses_frozen_preview_even_after_library_changes(self):
        import app
        from types import SimpleNamespace
        preview=g.injection_preview(["show"], [{"text":"Manual","weight":5}])
        settings={"asr_injected_hotwords":preview["hotwords"]}
        g.save_glossary_library([],[])
        with patch.object(app,"build_transcription_request",side_effect=lambda **kwargs:kwargs):
            request=app._workbench_transcription_request(SimpleNamespace(input_path=Path("media.wav"),job_dir=Path("project")),settings)
        self.assertEqual(request["hotwords"],{"Manual":5,"Nova":5})

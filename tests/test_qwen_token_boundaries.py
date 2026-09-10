import unittest
from substar_core.qwen_cloud_asr import _natural_qwen_units

class QwenTokenBoundaryTests(unittest.TestCase):
    def units(self, *texts):
        return _natural_qwen_units([dict(text=text, begin_time=i*100, end_time=(i+1)*100) for i,text in enumerate(texts)], {})[0]

    def test_native_boundaries_after_ellipsis_and_comma(self):
        for separator in ("...", ",", "…"):
            units = self.units("hello" + separator, "world")
            self.assertEqual([u["text"] for u in units], ["hello"+separator,"world"])
            self.assertEqual([u["start"] for u in units], [0, .1])
            self.assertTrue(all(u["timing_source"] == "qwen_cloud_native" for u in units))

    def test_fragmented_ellipsis_blocks_join(self):
        self.assertEqual([u["text"] for u in self.units("hello", ".", ".", ".", "world")], ["hello...", "world"])

    def test_punctuation_field_blocks_join(self):
        units, _, _ = _natural_qwen_units([dict(text="hello",punctuation=",",begin_time=0,end_time=100),dict(text="world",begin_time=200,end_time=300)], {})
        self.assertEqual([u["text"] for u in units], ["hello,", "world"])
        self.assertEqual(units[1]["start"], .2)

    def test_standalone_punctuation_attaches_to_previous(self):
        self.assertEqual([u["text"] for u in self.units("hello",",","world")], ["hello,","world"])

    def test_embedded_words_have_explicit_estimated_timing(self):
        for text in ("hello...world", "hello,world", "hello world"):
            units = self.units(text)
            self.assertEqual(len(units),2)
            self.assertEqual(units[0]["start"],0)
            self.assertEqual(units[-1]["end"],.1)
            self.assertEqual(units[0]["end"],units[1]["start"])
            self.assertTrue(all(u["timing_source"] == "qwen_cloud_estimated" for u in units))

    def test_lexical_punctuation_and_tokenizer_pieces_preserved(self):
        for text in ("3.14", "1,000", "U.S.", "don't", "https://example.com/a,b?q=x", "a.b@example.com"):
            self.assertEqual([u["text"] for u in self.units(text)],[text])
        self.assertEqual([u["text"] for u in self.units("V","uit","ton")],["Vuitton"])
        self.assertEqual([u["text"] for u in self.units("1","0",".","3")],["10.3"])

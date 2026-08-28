from __future__ import annotations

import unittest

from substar_core.qwen_enhancement import (
    normalize_qwen_hotwords,
    prioritize_generated_qwen_hotwords,
)


class QwenEnhancementTests(unittest.TestCase):
    def test_multilingual_hotwords_and_super_weight_are_preserved(self) -> None:
        self.assertEqual(
            normalize_qwen_hotwords([
                {"text": "英伟达", "weight": 4},
                {"text": "Blackwell", "weight": 5},
                {"text": "CUDA", "weight": 50},
            ]),
            [
                {"text": "英伟达", "weight": 4},
                {"text": "Blackwell", "weight": 5},
                {"text": "CUDA", "weight": 50},
            ],
        )

    def test_duplicate_hotwords_keep_the_first_user_value(self) -> None:
        self.assertEqual(
            normalize_qwen_hotwords([
                {"text": "CUDA", "weight": 5},
                {"text": "cuda", "weight": 4},
            ]),
            [{"text": "CUDA", "weight": 5}],
        )

    def test_official_capacity_is_accepted(self) -> None:
        rows = [{"text": f"term{i}", "weight": 4} for i in range(2000)]
        self.assertEqual(len(normalize_qwen_hotwords(rows)), 2000)

    def test_more_than_fifty_super_hotwords_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "最多 50"):
            normalize_qwen_hotwords([
                {"text": f"term{i}", "weight": 50} for i in range(51)
            ])

    def test_unsupported_weight_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "1–5 或 50"):
            normalize_qwen_hotwords([{"text": "Blackwell", "weight": 6}])

    def test_ai_weights_direct_terms_as_super_and_inferred_terms_as_strong(self) -> None:
        self.assertEqual(
            prioritize_generated_qwen_hotwords(
                [
                    {"text": "CUDA", "weight": 4},
                    {"text": "Tensor Core", "weight": 4},
                ],
                user_prompt="请重点识别 CUDA，并讨论 GPU 架构。",
            ),
            [
                {"text": "CUDA", "weight": 50},
                {"text": "Tensor Core", "weight": 5},
            ],
        )

    def test_ai_super_hotwords_never_exceed_official_limit(self) -> None:
        terms = [f"term{i}" for i in range(51)]
        result = prioritize_generated_qwen_hotwords(
            [{"text": term, "weight": 5} for term in terms],
            user_prompt=" ".join(terms),
        )
        self.assertEqual(sum(item["weight"] == 50 for item in result), 50)
        self.assertEqual(result[-1]["weight"], 5)


if __name__ == "__main__":
    unittest.main()

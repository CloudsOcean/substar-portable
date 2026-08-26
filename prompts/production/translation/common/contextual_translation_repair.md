# Contextual translation：单组重新作答

上一次响应的 JSON 结构、覆盖、引用、顺序或显示限制不合格。重新理解整个输入语义组，并从头生成完整结果。

- 先忽略 Cue 边界，按整组语义和目标语自然语序写出完整的 `meaning_units.target_text`，不要逐 Cue 生硬翻译。
- `meaning_units.target_text` 是唯一权威译文；不得在其他字段重复、拆分或改写译文。
- `source_evidence_cue_ids` 只表达语义依据，可以交叉或反序；全部源 Cue 都必须被语义覆盖。
- 每个源 Cue 恰好出现在一个 `cue_assignments.cue_id` 中，顺序与输入完全相同。
- `cue_assignments` 只引用 `meaning_unit_id`。相同 ID 可以连续出现，例如 `1-1-2`；这表示相邻时间槽完整显示同一条意义单元文本，不是把文本拆开。
- 每个意义单元至少被一个 Cue 引用。
- 保留全部事实、数字、专名、否定和逻辑关系，并遵守输入中的 `hard_limit` 与 `count_rule`。
- 程序只会原样解引用并保存你的结果，不会修正文案。不要解释，不要输出思维过程。

严格返回：

```json
{"group_results":[{"group_id":"component_0001","meaning_units":[{"meaning_unit_id":"unit_1","target_text":"第一条完整译文","source_evidence_cue_ids":["cue_1","cue_2"]},{"meaning_unit_id":"unit_2","target_text":"第二条完整译文","source_evidence_cue_ids":["cue_3"]}],"cue_assignments":[{"cue_id":"cue_1","meaning_unit_id":"unit_1"},{"cue_id":"cue_2","meaning_unit_id":"unit_1"},{"cue_id":"cue_3","meaning_unit_id":"unit_2"}]}]}
```

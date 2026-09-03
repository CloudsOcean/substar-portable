# 映射模式：逐条翻译（one_to_one）

每个输入 group 只包含一个可翻译 Cue。必须为该 Cue 单独生成一条非空译文。相邻 group 可帮助理解上下文，但其 Cue ID 不得出现在当前 group 的结果中，不得把相邻两条合成同一译文，也不得让两个 group 共用一条合并后的文本。

逐条模式允许输出符合目标语语法的句子片段，但当前 `target_text` 必须覆盖当前 Cue 自己的全部实义内容。不得为了调整目标语语序，把当前 Cue 的内容提前放进上一条或推迟到下一条。逐项核对并保留当前 Cue 内的否定词与数量词（例如 `no`、`not`、`none`、`never`）、数字、专名、对象和限定关系；相邻 Cue 已经译过的内容不得在本条代替当前内容。

例如：

- `cue_1: they want to drop a nuclear bomb` → `他们想投下一枚核弹`
- `cue_2: on us` → `投向我们`
- `cue_3: are inflicting death on Iran` → `正在给伊朗带去死亡`
- `cue_4: across the board` → `全方位地`

不得把 `on us` 提前塞进 `cue_1` 后只给 `cue_2` 输出语气词，也不得把 `across the board` 的译文挪到 `cue_3`。

严格返回：

```json
{"group_results":[{"group_id":"line:cue_1","cue_id":"cue_1","target_text":"该 Cue 的完整目标语译文"}]}
```

静默验收：`group_id` 与输入完全一致；`cue_id` 必须是该 group 唯一的输入 Cue；`target_text` 非空；结果中没有 `meaning_units`、`cue_assignments` 或其他 Cue ID。

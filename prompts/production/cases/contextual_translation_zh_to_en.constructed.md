# Contextual translation Chinese→English contrast cases

1. Natural coordination:

Input: `1 我们必须结束这场争端` / `2 恢复正常贸易`

Preferred: `1-2|We must end this dispute and restore normal trade.`

2. Do not strand English function words:

Input: `1 如果河水再次上涨` / `2 北部公路` / `3 将在午夜前关闭`

Preferred:

`1|If the river rises again,`

`2-3|the northern road will close before midnight.`

3. Preserve negation scope:

Input: `1 该机构并未称桥梁不安全` / `2 只是表示` / `3 需要再次检查`

Preferred:

`1|The agency did not say the bridge was unsafe.`

`2-3|It only said another inspection was needed.`

4. Preserve every quantity once and reorder naturally when needed. Never invent a subject, gender, organization, or relationship omitted by the source.

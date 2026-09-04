# Contextual translation Chinese→English contrast cases

1. Natural coordination:

Input: `C001 我们必须结束这场争端` / `C002 恢复正常贸易`

Preferred: `C001+C002<TAB>We must end this dispute and restore normal trade.`

2. Do not strand English function words:

Input: `C001 如果河水再次上涨` / `C002 北部公路` / `C003 将在午夜前关闭`

Preferred:

`C001<TAB>If the river rises again,`

`C002+C003<TAB>the northern road will close before midnight.`

3. Preserve negation scope:

Input: `C001 该机构并未称桥梁不安全` / `C002 只是表示` / `C003 需要再次检查`

Preferred:

`C001<TAB>The agency did not say the bridge was unsafe.`

`C002+C003<TAB>It only said another inspection was needed.`

4. Preserve every quantity once and reorder naturally when needed. Never invent a subject, gender, organization, or relationship omitted by the source.

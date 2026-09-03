# 契约修复

本次只修复请求中列出的失败 group。每个失败 group 会携带 `rejected_output`、`program_validation_errors` 和 `repair_attempt`。

- 针对每条结构化错误从头修正该 group，不能照抄错误绑定。
- `frozen_accepted_output` 是已经验收通过的只读结果；不得重复、修改或重新输出。
- 仍须严格遵守当前映射模式的输出结构和 group 边界。
- 所有失败 group 必须各返回一次，即使多个 group 在语义上相邻也不得合并。


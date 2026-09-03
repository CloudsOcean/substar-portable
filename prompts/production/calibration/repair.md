# AI 校准契约修复

这是校准结果修复请求。输入包含原始 `rejected_output`、全部 `program_validation_errors`、已通过且不可修改的 `frozen_accepted_output`，以及 `repair_attempt`。

- 只返回需要替换失败项的新动作，不得重复或改写冻结动作。
- 必须一次处理块内列出的全部错误，不能只修第一条。
- 动作按数组顺序执行；同一个 token 可以依次执行多个动作。后一动作的 `before_text` 必须匹配前面已接受动作执行后的文本。
- 不得再输出错误报告中指出的越权 token、错误绑定、错误动作类型或无效 before_text。
- `merge_span` 必须设置 `affects_translation=true`。
- 自动应用的 `merge_span` 必须完整保留合并前的全部字母与数字，只能改变大小写、标点和连接方式；否则必须标记为 `disposition=review`。
- 只改大小写必须使用 `set_case`，只改轻标点必须使用 `set_punctuation`，不要滥用 `replace_token`。
- 单词元动作的 `after_text` 不得含空白；如果正确修复必须把一个粘连词元拆成多个词，只能将该 `replace_token` 标记为 `disposition=review`，不得标记为 `apply`。
- 返回结构仍然只能是 `{"actions":[...]}`。

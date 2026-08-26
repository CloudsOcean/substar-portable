# Contextual translation 中译英构造案例

Cases demonstrate joint translation and cross-Cue allocation only. `Weak` outputs are failure patterns; `Preferred` outputs show a natural continuous allocation within one meaning group. Do not output an analysis trace.

1. **Natural coordination**: `我们必须结束这场争端 / 恢复正常贸易`. Weak: `We must end this dispute / restore normal trade.` Preferred: `We must end this dispute / and restore normal trade.` `N:M, none`.
2. **Long modifier reorder**: `经过数月协商 / 委员会批准了 / 当地工程师提出的方案`. Weak: mechanically preserve every Chinese boundary. Preferred: `After months of consultation / the committee approved a plan / proposed by local engineers.` `N:M, full`.
3. **Do not strand English function words**: `如果河水再次上涨 / 北部公路 / 将在午夜前关闭`. Weak: `If the river rises again / the northern road will / close before midnight.` Preferred: `If the river rises again / the northern road / will close before midnight.` `N:M, partial`.
4. **Negation scope**: `该机构并未称桥梁不安全 / 只是表示 / 需要再次检查`. Weak: `The agency said the bridge was not unsafe / only / another inspection was needed.` Preferred: `The agency did not say the bridge was unsafe / it only said / another inspection was needed.` `N:M, partial`.
5. **Quantity and recipient order**: `未来三年 / 该基金将向42家诊所 / 提供1800万美元`. Preferred: `Over the next three years / the fund will provide $18 million / to 42 clinics.` Preserve every quantity once. `N:M, full`.
6. **Omitted subject**: `预计 / 明年春季 / 正式投入运行`. Preferred only when the actor is genuinely unspecified: `It is expected / to begin operations / next spring.` Never invent a company, government, gender, or person. `N:M, full`.
7. **把 construction**: `我们把新的安全方案 / 提交给了 / 审查委员会`. Weak: `We the new safety plan / submitted to / the review committee.` Preferred: `We submitted / the new safety plan / to the review committee.` `N:M, full`.
8. **Short Cue and hard limit**: `简而言之 / 这项方案成本太高 / 收效太低`. Preferred: `In short / the proposal costs too much / and delivers too little.` Do not duplicate the full sentence in the short first Cue. `N:M, none`.

9. **Two Chinese Cues to one English meaning unit (1-1)**: `c1 直到最终表决后 / c2 这项任命才得到确认`. Use the same `meaning_unit_id="unit_1"` for both Cue outputs, but author two non-duplicated final fragments such as `c1 The appointment was not confirmed / c2 until after the final vote.` Never copy the complete sentence into both Cues.
10. **One Cue remains one time slot**: `c1 委员会批准了当地工程师提出的方案`. Return one meaning unit whose `target_text` is `The committee approved the plan proposed by local engineers.`, and one Cue assignment that references it. The program does not split time or create additional Cues.
11. **Partial reorder**: `A=该机构表示 / B=检查结束后 / C=需要立即 / D=再次审查` may become `A-B-D-C`: `The agency said / after the inspection / another review / was needed immediately.` Only a local order changes, so use `partial`.
12. **Full reorder**: `A=未来三年 / B=该基金将向42家诊所 / C=提供1800万美元` may become `A-B+C` with object/recipient redistribution: `Over the next three years / the fund will provide $18 million / to 42 clinics.` Use `full` when the main information order is reorganized.

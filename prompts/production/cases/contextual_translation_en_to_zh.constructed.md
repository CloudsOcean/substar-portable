# Contextual translation 英译中构造案例

案例只说明整组推理、意义单元分配和最终 Cue 输出，不得套用其中事实或译文。

1. **先整组理解，不按源 Cue 切碎**：输入 `c1 We must end this dispute / c2 and restore normal trade`。形成唯一完整意义单元“我们必须结束这场争端并恢复正常贸易”，再分配 `c1→unit_1 / c2→unit_1`。错误做法是新建“我们必须结束这场争端”与“恢复正常贸易”两个 unit；它们虽能勉强连读，却没有必要破坏一个不超限的完整并列谓语。

2. **目标语自然语序优先，长度不决定合并**：`c1 The committee approved a plan / c2 proposed by local engineers / c3 after months of consultation`。中文先形成“经过数月协商，委员会批准了当地工程师提出的方案”，再按中文的信息推进规划显示片段。`hard_limit` 只用于否决过长片段；不能因为整句未超限就直接决定 `1-1-1`，也不得照源语序制造“委员会批准了一项方案 / 由当地工程师提出 / 经过数月协商”。

3. **同一支付事件按中文语序生成**：

源 Cue：

- `c1 so the company was ordered to pay`
- `c2 10.3 million RMB`
- `c3 within ten days`

对象、金额和期限都属于同一支付事件。不要按英文 Cue 依次翻译，也不要靠逗号粘合三条机械译文。可以按中文语序输出：

```json
{
  "meaning_units": [
    {"meaning_unit_id":"unit_1","target_text":"该公司被责令在10天内","source_evidence_cue_ids":["c1","c3"]},
    {"meaning_unit_id":"unit_2","target_text":"支付1030万元人民币","source_evidence_cue_ids":["c1","c2"]}
  ],
  "cue_assignments": [
    {"cue_id":"c1","meaning_unit_id":"unit_1"},
    {"cue_id":"c2","meaning_unit_id":"unit_2"},
    {"cue_id":"c3","meaning_unit_id":"unit_2"}
  ]
}
```

这是中文语序决定的 `1-2-2`：期限被放入前一中文成分，金额进入后一成分；后一完整片段跨两个时间槽持续显示。

4. **同一意义单元跨多槽位**：源文两条 Cue 合起来表达 `Molly Tea will appeal the decision` 时，只建立“茉莉奶白将对该判决提出上诉”一个 unit，并让两个 Cue 都引用它。不得输出 `茉莉奶白 / 将对该判决提出上诉`，更不得输出 `茉莉奶 / 白将对……`。

5. **禁止悬空边界**：`Louis Vuitton does not / know the Chinese market well enough` 应只有“路易威登不够了解中国市场”一个 unit，并分配 `1-1`。`路易威登 / 对中国市场不够了解` 与 `路易威登对 / 中国市场不够了解` 都是不合格的逐 Cue 碎片。

6. **否定范围**：`c1 The agency did not say / c2 the bridge was unsafe / c3 only that another inspection was needed` 应规划为 `unit_1 该机构并未声称桥梁存在安全问题` 与 `unit_2 只是表示还需再次检查`，分配 `1-1-2`。不得把否定范围拆成三个逐 Cue unit。

7. **术语与名称**：术语表指定 `Molly Tea → 茉莉奶白` 时必须使用“茉莉奶白”。若上下文讨论名称来源，可写成 `英文名“Molly”源自中文“茉莉”`，不得无引号地拆成 `Molly / Moli / Jasmine`。
8. **跨 Cue 的完整目标语意义**：输入 `c19 you can often see the design / c20 used in Chinese architecture / c21 especially on window lattices and door canopies` 时，先形成 `unit_1 你常能看到这种图案用于中国建筑中` 和 `unit_2 尤其是窗棂和门檐上`，再分配 `c19→unit_1 / c20→unit_1 / c21→unit_2`。前两个时间槽完整重复 unit_1；不得把它拆成“你常能看到这种图案 / 用于中国建筑中”。
9. **跨 Cue 的从句与论据**：输入 `c33 that Louis Vuitton does not / c34 know their Chinese market well enough / c35 because Dior hasn't even sued Chagee yet.` 时，先形成 `unit_1 路易威登不够了解中国市场` 和 `unit_2 因为连迪奥都还没起诉霸王茶姬`，再分配 `c33→unit_1 / c34→unit_1 / c35→unit_2`，即 `1-1-2`。不得生成悬空的“路易威登并不 / 足够了解……”；完整谓语应在前两个时间槽持续显示。

10. **不合格逐 Cue 输出对照**：`c1 many users are already jumping in / c2 to provide new logo options`。
    - 不合格：两个 unit“许多用户已经纷纷加入”/“提供新标志方案”。前者缺少加入什么，后者缺少主语，明显来自源 Cue 边界。
    - 合格：一个 unit“许多用户已纷纷给出新的标志方案”，分配 `1-1`。

11. **不要盲目最少化**：若整组包含两个可以各自成立、各自承担新信息的完整命题，例如“法院判决原告胜诉”与“被告将提起上诉”，应保留两个 unit。最少化的对象是无意义的字幕碎片，不是删除必要的语义层次。

12. **原因与方式依附同一事件**：`c1 for infringing the brand's trademark / c2 by using its signature four-leaf clover design` 依附于前文同一被诉事件。中文可以根据长度生成 `涉事公司因使用标志性的四叶草设计 / 而被诉侵犯该品牌商标权`；其中“涉事公司”必须替换为上下文中唯一确定的真实主体。不得生成互不相干的“侵犯商标 / 通过使用设计”。

13. **显化省略的逻辑主语**：`c1 the name means jasmine / c2 hence the floral association` 中，第二条的逻辑主语来自前文。中文应写成“这个名称因此与花卉联系起来”等自然表达，不得只写“因此有花卉联想”。

14. **条件—结果保留边界**：`c1 If the appeal is unsuccessful / c2 the company will need to change its logo` 应生成“如果上诉失败 / 该公司就需要更换标志”。条件从句有明确语法身份，可以作为一个意义单元；不得为了减少 unit 而吞掉条件关系。

15. **补充说明可以保留**：`has more than a thousand years of history / since the Tang Dynasty` 的后半部分限定时间起点。中文可生成“已有上千年历史 / 至少可追溯到唐代”，也可以自然重组为“自唐代距今已有上千年历史”；选择取决于中文表达和显示长度，而不是源 Cue 数量。

16. **信息推进时果断更新字幕**：以下边界都来自自然中文的信息推进，即使合并后不超过 `hard_limit`，也应建立不同 unit：
    - `many Chinese netizens are saying / that the design appeared in China very early` → `许多中国网友表示 / 这种图案在中国很早就出现了`；
    - `and has more than a thousand years of history / since the Tang Dynasty` → `而且至少已有上千年的历史 / 可追溯到唐代`；
    - `Chinese netizens also say / that Louis Vuitton does not / know the Chinese market well enough` → `中国网友也表示 / 路易威登不够了解中国市场 / 路易威登不够了解中国市场`，即 `1-2-2`。

17. **没有有效推进时保持显示**：不要为了增加 unit 数量而切分不可自然再分的中文片段。`you can often see the design / used in Chinese architecture` 可持续显示“你常能看到这种图案用于中国建筑中”；`many users are already jumping in / to provide new logo options` 可持续显示“许多用户已纷纷给出新的标志方案”。判断依据是目标语显示内容是否应当向前推进，不是源 Cue 数量，也不是离 `hard_limit` 还有多少空间。

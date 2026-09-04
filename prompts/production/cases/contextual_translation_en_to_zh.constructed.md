# Contextual translation 英译中构造案例

案例只说明目标语重组和 C 别名映射，不得套用其中事实或译文。同一译文跨多个时间槽时只能写一次。

1. **整块理解，不按源 Cue 切碎**

输入：`C001 We must end this dispute` / `C002 and restore normal trade`

合格：`C001+C002<TAB>我们必须结束这场争端并恢复正常贸易`

不合格：分别输出缺主语或缺谓语的碎片；也不合格：为 C001、C002 重复写两遍同一译文。

2. **目标语自然语序优先**

输入：`C001 The company was ordered to pay` / `C002 10.3 million RMB` / `C003 within ten days`

可按中文信息推进输出：

`C001<TAB>该公司被责令在10天内`

`C002+C003<TAB>支付1030万元人民币`

每个 C 别名只出现一次；`+` 表示相邻槽位共享右侧这一份完整译文，不得复制右侧文本。

3. **否定范围完整保留**

输入：`C001 The agency did not say` / `C002 the bridge was unsafe` / `C003 only that another inspection was needed`

合格：

`C001+C002<TAB>该机构并未声称桥梁存在安全问题`

`C003<TAB>只是表示还需再次检查`

4. **禁止悬空边界**

输入：`C001 Louis Vuitton does not` / `C002 know the Chinese market well enough`

合格：`C001+C002<TAB>路易威登不够了解中国市场`

不合格：`路易威登并不 / 足够了解……`；不合格：分别为 C001、C002 输出相同译文。

5. **自然推进时及时更新**

输入：`C001 many Chinese netizens are saying` / `C002 that the design appeared in China very early`

合格：

`C001<TAB>许多中国网友表示`

`C002<TAB>这种图案在中国很早就出现了`

是否推进由目标语信息结构决定，不由旧式意义组或离字符上限的距离决定。

6. **术语与专名**

术语表指定 `Molly Tea → 茉莉奶白` 时必须使用“茉莉奶白”。事实、数字、否定、条件和指代不得因压缩或重组而丢失。

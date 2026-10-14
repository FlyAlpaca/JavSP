# 演员名称资源

`kanji-variants.tsv` 是通用字符数据，由 OpenCC `ver.1.2.0` 的
[JPVariants.txt](https://github.com/BYVoid/OpenCC/blob/ver.1.2.0/data/dictionary/JPVariants.txt)
反向生成，并补充东京大学史料编纂所
[异体字同定表](https://wwwap.hi.u-tokyo.ac.jp/ships/itaiji_list)中 `嶋/嶌 → 島` 的字符对应。
OpenCC 数据采用 Apache-2.0，许可证保存在 `LICENSE.OpenCC`。
歧义字的多个候选只有在简繁归一后相同时才参与转换。

`actor-supplement.xml` 保存上游缺失的姓名数据及明确的新旧艺名译名对应关系，
每个父条目必须带 `source`。这份数据不修改下载缓存；新增演员只用于名称展示。
已有有效上游中文字段和上游显式 `alias` 译名优先；冲突按通用规则保留输入。
父条目只有 `alias`、没有有效主名中文字段时，必须先在上游找到父条目，避免创建孤立身份。

`a` 的 `jp/zh_cn/zh_tw/keyword` 与上游 XML 相同；可用子节点
`<alias jp="别名" zh_cn="对应中文" />` 为某个别名提供专属译名。
该格式同样适用于上游数据和 `ActorMapping.from_xml(..., supplement=...)` 的补充输入。
没有显式对应关系时，不从 keyword 自动猜测新旧艺名之间的专属译名对应，仍沿用主名中文。

当前缺失条目采用两个中文资料页均收录的“和香夏月”；其他页面也存在“和香夏树”写法，
数据选择及出处留在 XML 中，后续可更正数据，无须改变解析算法。

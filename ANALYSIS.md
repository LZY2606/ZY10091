# Marko 扩展生命周期与上下文泄漏分析

分析基线：仓库快照 `c5f535e`（v2.2.4）。新增测试为 `tests/test_source_context.py` 与
`tests/test_renderer_context.py`。本文所有行号对应当前工作树（修复后），路径均为仓库相对路径。

## 1. 扩展如何合并：`Markdown.use` 与 setup

### 1.1 注册阶段：只累积，不构建

`Markdown.__init__` 建立三个空容器：`_parser_mixins`、`_renderer_mixins`、`_extra_elements`
（`marko/__init__.py:60-67`）。`use()` 对每个扩展做三件事（`marko/__init__.py:72-91`）：

1. 字符串名经 `load_extension` 动态导入并调用 `make_extension()`
   （`marko/helpers.py:118-139`），得到 `MarkoExtension` 数据类
   （`marko/helpers.py:112-116`，只含 `parser_mixins/renderer_mixins/elements` 三个列表）。
2. **前插** mixin：`self._parser_mixins = extension.parser_mixins + self._parser_mixins`
   （`marko/__init__.py:89`），renderer 同理（`:90`）。
3. **后追加** elements：`self._extra_elements.extend(extension.elements)`（`:91`）。

`use()` 在 `_setup_done` 后调用会抛 `SetupDone`（`:83-84`），保证构建后不可再注册。

### 1.2 构建阶段：动态子类 + MRO + 元素表覆盖

`_setup_extensions()` 在首次 `parse()`/`render()` 时惰性执行一次（`marko/__init__.py:93-111`、
`:120-135`）：

- Parser：`type("_Parser", tuple(self._parser_mixins) + (self._base_parser,), {})()`
  （`:98-100`）。
- Renderer：同样把全部 renderer mixin 放在 base renderer 之前（`:103-108`）。
- 元素：构造完基础 `Parser`（其 `__init__` 已注册 CommonMark 全集，`marko/parser.py:28-34`）
  后，对每个扩展元素调用 `self.parser.add_element(e)`（`marko/__init__.py:101-102`）。

优先级因此有两套互相独立的机制：

**Block 元素优先级。** `add_element` 按 `element.get_type()` 写入字典
（`marko/parser.py:36-54`）。`get_type()` 在 `override=True` 且父类不是
`BlockElement/InlineElement` 时返回**基类名**（`marko/element.py:133-147`），因此扩展元素与
被替换元素占用同一个 key，即“替换”。解析时 `_build_block_element_list` 按类属性
`priority` 降序排序（`marko/parser.py:138-143`，数值越大越早尝试），`parse_source` 依次调用
`ele_type.match(source)`，第一个命中者消费输入（`marko/parser.py:71-89`）。GFM 示例：
`Alert` 显式取 `Quote.priority + 1`（`marko/ext/gfm/elements.py:262`）抢在 `Quote`
（`marko/block.py:490`，priority=6）之前；GFM `Paragraph(override=True)` 替换基础
Paragraph 但继承 priority=1（`marko/ext/gfm/elements.py:12-25`，基础值 `marko/block.py:384`）。

**Inline 元素优先级。** inline 元素不排序，按插入顺序 `find()`（`marko/inline_parser.py:71-73`），
命中的 token 排序后用 `_resolve_overlap` 仲裁；交集时 `prev.etype.priority < cur.etype.priority`
后者胜（`marko/inline_parser.py:79-94`），包含关系则由外层收编内层（`CONTAIN` 分支）。

**Renderer 优先级 = MRO。** mixin 前插使其在 MRO 中位于 base renderer 之前；先注册的扩展
最终位于 MRO **更后**（更靠近 base），因此**后注册的 mixin 方法先被找到**。元素注册顺序
（后追加 + 同 key 覆盖）与 MRO 结论一致：**后 `use()` 的扩展生效**。这与 `use()` docstring
声称的 “An extension preceding in order will have higher priorty”（`marko/__init__.py:81`）
相反；实际行为已被 `tests/test_basic.py:104-113`（`test_extension_override`，用户 mixin 在
`gfm` 之后注册并胜出）钉住。

## 2. Parse：`Source` 的 state / anchor / match / 前瞻

`Parser.parse` 每次新建 `Source`（`marko/parser.py:56-58`），所以所有逐解析状态天然不会跨
`parse()` 调用共享。`Source.__init__` 初始化 `pos=0`、`_anchor=0`、`_states=[]`、
`match=None`、以及一个全新的 `context = SimpleNamespace()`（`marko/source.py:29-37`）。

- **state 栈。** `push_state/pop_state`（`marko/source.py:53-59`）；
  `under_state` 是唯一带异常安全边界的结构——`try/finally` 保证 `pop_state()` 必定执行
  （`marko/source.py:61-67`）。`prefix` 属性由栈上各元素的 `_prefix` 拼接（`:69-71`），
  `_update_prefix` 在消费换行后把含 `_second_prefix` 的状态（ListItem/FootnoteDef）切换到
  续行前缀（`:168-171`）。
- **anchor。** `anchor()` 钉住 `pos`（`marko/source.py:160-162`），`reset()` 回到锚点
  （`:164-166`）。锚点用于 List/CodeBlock/Table 的前瞻回退，但**不在** `under_state` 的
  恢复范围内（它只管 state 栈）。
- **match 与前瞻。** `expect_re` 先按当前 `prefix` 计算行内偏移再匹配，并把结果写入
  `self.match`（`marko/source.py:114-130`）；`next_line` 是它的薄封装（`:136-149`）；
  `consume` 把 `pos` 推进到 `match.end()`、必要时更新前缀、然后清空 `match`（`:152-158`）。
  即 `match` 是“最近一次前瞻”的共享暂存，跨元素复用，没有独立栈。
- 一个额外的手工恢复点：`Paragraph.break_paragraph` 用 `try/finally` 把 `source.match`
  还原为进入前的值（`marko/block.py:405-437`），因为它在判断段落是否可被打断时会借用其他
  block 元素的 `match()`，不能污染调用方的前瞻。
- 另一个手工恢复点：`Paragraph.parse` 的懒续行分支直接快照 `states = source._states[:]`、
  手工 `pop_state()`、结束后整体写回（`marko/block.py:465-484`）。这段**不经过**
  `under_state`，若循环体内部抛异常，写回不会发生；由于 `Source` 是一次性对象，影响仅限
  当前这次失败的 `parse()`，不会泄漏到下一次调用。

## 3. Render：dispatcher 与 context manager

### 3.1 方法选择

- `Renderer.render` 以 `render_` + `element.get_type(snake_case=True)` 取名，再 `getattr`
  查找（`marko/renderer.py:57-80`）；找到且函数带 `_force_delegate` 或类属性 `delegate=True`
  才调用，否则回落 `render_children`（`:82-94`）。`ASTRenderer/XMLRenderer` 设
  `delegate=False`，只有 `@force_delegate` 的方法生效（`marko/ast_renderer.py:31-34`）。
- 扩展可用 `@render_dispatch(SomeRenderer)` 定义按 renderer 类型分发的描述符
  （`marko/helpers.py:142-209`）。`_RendererDispatcher.__get__` 用 `isinstance(obj, types)`
  顺序查 `_mapping`（默认还登记 `(ASTRenderer, XMLRenderer) -> render_ast`，
  `marko/helpers.py:160-168`），都不匹配时回落 `super_render`，沿 MRO 找下一个同名方法
  （`marko/helpers.py:174-184`）。这就是同一个 footnote 元素在 HTML / Markdown / AST
  三种 renderer 下被不同方法处理的机制。

### 3.2 context manager

`Markdown.render` 用 `with self.renderer as r:` 包裹整次渲染（`marko/__init__.py:128-135`）。
基类 `Renderer.__enter__` 把 `(html._charref, self.root_node)` 压入 `_context_stack` 并把
`html._charref` 临时替换为 marko 自己的字符引用正则；`__exit__` 无条件 `pop()` 恢复
（`marko/renderer.py:48-55`）。因为 `__exit__` 不检查异常参数，异常路径下
`html._charref` 与 `root_node` 也会恢复——这正是 `tests/test_renderer_context.py:9-30`
钉住的不变量（含同一 renderer 实例嵌套复用与内层抛异常两个维度）。

子类的额外状态：

- `MarkdownRenderer.__enter__` 重置 `_prefix/_second_prefix`（`marko/md_renderer.py:29-31`），
  其 `container()` 用 try 之外保存/末尾手写恢复的方式切换前缀（`marko/md_renderer.py:36-47`）——
  异常时 `yield` 跳出，前缀不会恢复；但下一次顶层 `render` 的 `__enter__` 会重新清零，
  所以不跨调用泄漏。GFM alert 的 Markdown 渲染借用它（`marko/ext/gfm/renderer.py:120-127`）。
- `TocRendererMixin.__enter__` 每次进入清空 `headings`（`marko/ext/toc.py:39-41`）。
- `LatexRendererMixin` 用 `_packages_back` 在 `__exit__` 还原包集合
  （`marko/ext/latex_renderer.py:21-27`）。
- `FootnoteRendererMixin.__init__` 建 `self.footnotes = []`
  （`marko/ext/footnote.py:70-72`），`render_document` 在**正常返回前**清空
  （`marko/ext/footnote.py:113`）。它没有对应的 `__enter__` 重置，是第 5 节候选 B 的根源。

## 4. 三条完整链路

### 链 1：普通段落含 emphasis（`a *b* c`）

1. `Markdown.convert` → `parse` 触发 setup（`marko/__init__.py:113-126`）。
2. `Parser.parse` 建 `Source`、`Document`，`under_state(doc)` 下调 `parse_source`
   （`marko/parser.py:56-68`；状态边界 `marko/source.py:61-67`）。
3. block 列表按 priority 降序（`marko/parser.py:138-143`）：FencedCode(7)、Heading/Quote/
   List(6)、BlankLine(5)、CodeBlock(4) 都不匹配，最后 `Paragraph.match`
   （`marko/block.py:397-399`，pattern `[^\n]+`）命中。
4. `Paragraph.parse`（`marko/block.py:439-485`）：`next_line`+`consume`
   （`marko/source.py:136-158`）；循环中 `break_paragraph` 借其他元素前瞻，其
   `try/finally` 恢复 `source.match`（`marko/block.py:405-437`）。返回 `lines` 后由
   `parse_source` 包成 `Paragraph` 并写 `source_span`（`marko/parser.py:75-84`）。
5. inline 解析延后到 `parse_inline`（`marko/parser.py:95-105`），最终到
   `inline_parser.parse`：`find_links_or_emphs` 扫描分隔符栈
   （`marko/inline_parser.py:213-250`），`process_emphasis` 按左右侧翼与 `closed_by`
   规则配对 `*`（`marko/inline_parser.py:386-432`），产出虚拟元素
   `Emphasis`（`marko/inline.py:148-152`）；token 重叠仲裁在
   `marko/inline_parser.py:79-94`。
6. `Markdown.render` 进入 renderer 上下文（`marko/renderer.py:48-55`）；
   `render_document → render_paragraph → render_children → render_emphasis`
   （`marko/html_renderer.py:17-24`、`:88-90`）。

**异常边界**：解析侧唯一保证是 `under_state` 的 finally（doc 状态必定弹出）与
`break_paragraph` 的 match 还原；渲染侧是 `Renderer.__exit__`（html 正则与 root_node
必定恢复）。

### 链 2：GFM table 嵌套在 quote（`> | a | b |\n> | - | - |\n> | 1 | 2 |`）

1. block 优先级仲裁中 `Alert`（GFM，priority=7，`marko/ext/gfm/elements.py:262`）先试、
   不匹配；随后基础 `Quote.match`（`marko/block.py:497-499`）匹配 `>`。
2. `Quote.parse` 建 `Quote` 状态并入栈（`marko/block.py:501-506`），
   `prefix` 变为 ` {,3}>[^\n\S]?`（`marko/block.py:492`、`marko/source.py:69-71`）。
3. 递归 `parse_source`：GFM `Table.match` 先 `anchor()`，读首行并经
   `TableRow.match` 解析单元格（`marko/ext/gfm/elements.py:196-231`），再前瞻第二行确认
   分隔符；不是表时 `reset()` 回锚点返回 False（`marko/ext/gfm/elements.py:123-155`）。
   确认是表后 `Table.parse` 再 `under_state(rv)` 入栈读后续行
   （`marko/ext/gfm/elements.py:157-194`），此时状态栈为
   `[Document, Quote, Table]`，prefix 含 quote 前缀，单元格位置经 `_current_pos`
   （`marko/source.py:73-82`）与 `TableCell` 的 `_SourceMap` 映射回原始偏移
   （`marko/ext/gfm/elements.py:234-258`）。
4. 渲染：GFM mixin 的 `render_table/render_table_row/render_table_cell`
   （`marko/ext/gfm/renderer.py:59-87`）包在基础 `render_quote`
   （`marko/html_renderer.py:40-42`）里，产出 `<blockquote><table>…`（已实证，见第 6 节）。

**异常边界**：Quote 与 Table 两层 `under_state` 的 finally 各自弹栈，异常下状态栈仍
精确回到 `[Document]`；`Table.match` 的失败分支用 `anchor/reset` 回退 `pos`
（`marko/ext/gfm/elements.py:124,151`），但 `match`/`context` 不回滚——它们只服务当前
`Source`。备注：GFM 中直接放在 list item 里的 table 因前缀/打断规则不会被识别为 Table
（实测退化为 Paragraph，见第 6 节），这是解析语义，不是上下文泄漏。

### 链 3：footnote（+ 自定义元素）被不同 renderer 处理

输入 `see[^a]\n\n[^a]: body text\n`，扩展注册三个元素：`Document(override)`、
`FootnoteDef`（block，priority=6）、`FootnoteRef`（inline，priority=6）
（`marko/ext/footnote.py:24-67`，`make_extension` 在 `:116-121`）。

1. `FootnoteDef.match/parse` 用自身 `_prefix/_second_prefix` 入栈递归解析定义体，
   并写入 `source.root.footnotes`（`marko/ext/footnote.py:41-52`；root 访问
   `marko/source.py:47-51`）。
2. `FootnoteRef.find` 只产出**已在 `source.root.footnotes` 登记**的引用
   （`marko/ext/footnote.py:61-67`）；这依赖 inline 解析延后到所有 block 看完
   （`marko/parser.py:95-97` 的注释说明）。
3. 渲染时分发器按 renderer 类型选方法（`marko/helpers.py:186-209`）：
   - HTMLRenderer：`render_footnote_ref` 累积序号到 `self.footnotes`
     （`marko/ext/footnote.py:74-83`），`render_document` 末尾拼接脚注列表并清空
     （`:105-114`）。
   - MarkdownRenderer：`.dispatch(MarkdownRenderer)` 注册的版本输出 `[^a]` /
     `[^a]: …`（`:85-95`），且 `render_document` 中 `not isinstance(self, HTMLRenderer)`
     不拼脚注区（`:109-114`）。
   - ASTRenderer/XMLRenderer：描述符默认映射到 `render_ast` → `render_children`
     （`marko/helpers.py:160-168`）。
   自定义元素同理：任何 renderer mixin 或 `render_dispatch` 注册的方法都按
   isinstance + MRO 选择（第 3.1 节）。

**异常边界**：解析侧仍是 `under_state`；渲染侧 `Renderer.__exit__` 恢复
`html._charref/root_node`，但 **`FootnoteRendererMixin.footnotes` 不在恢复范围内**——
清空只发生在 `render_document` 正常返回时（候选 B）。

## 5. 四个可复现的风险候选

| # | 类别 | 位置 | 状态 |
|---|------|------|------|
| A | renderer 嵌套/复用 | `render_image` 临时替换 `self.render` 无 try/finally，`marko/html_renderer.py:108-118` | **已新增测试并修复** |
| B | renderer 嵌套（扩展） | `FootnoteRendererMixin.footnotes` 只在 `render_document` 正常结束时清空，`marko/ext/footnote.py:70-114` | 需要新增测试（未修复，本次不改） |
| C | extension 顺序 | `use()` docstring 称先注册优先，实际后注册优先（docs 已写对）；`marko/__init__.py:81` vs `:89-102` | 实际行为已被现有测试钉住，仅 docstring 需修 |
| D | source map / parse 状态 | `Paragraph.parse` 懒续行手工快照/写回 `_states`，不经 `under_state`，`marko/block.py:465-484` | 仅推测（异常只影响一次性 Source，无跨调用泄漏） |

### 候选 A（已修复，已加证明测试）

`HTMLRenderer.render_image` 为把 alt 渲染成纯文本，做
`render_func = self.render; self.render = self.render_plain_text; body = self.render_children(...)`，
然后在**普通语句**里恢复（修复前 `marko/html_renderer.py:108-117`）。alt 子树渲染一旦抛
异常，恢复语句不执行，实例方法永久变成 `render_plain_text`，而 `Renderer.render` 的方法名
分派（`marko/renderer.py:70-80`）随之整体失效。

纯公开扩展面复现（自定义 renderer 子类即可，无需碰私有属性）：

```python
class StrictRenderer(HTMLRenderer):
    def render_plain_text(self, element):
        if "boom" in getattr(element, "children", ""):
            raise ValueError("forbidden alt text")
        return super().render_plain_text(element)

md = Markdown(renderer=StrictRenderer)
md.convert("![boom](x.png)")   # raises ValueError
md.convert("# heading")        # 修复前: 'heading'（<h1> 丢失，实例已中毒）
```

修复前实测第二次输出 `'headingnormal bold text'`（标签全部消失）；修复后为
`<h1>heading</h1>…`。修复仅把恢复动作放进 `finally`（`marko/html_renderer.py:113-116`），
成功路径的输出字节级不变，未改变任何公开语义。最小证明测试：
`tests/test_renderer_context.py:34-63`
（`test_render_image_restores_dispatch_after_exception`）。该测试在回退修复后实测失败、
加回修复后通过；全量套件 1437 passed。

### 候选 B（真实可复现，建议后续补测试与修复）

`self.footnotes` 在 `__init__` 建立、`render_document` 末尾清空
（`marko/ext/footnote.py:72,113`），没有 `__enter__` 重置。若脚注定义体渲染中途抛异常，
标签列表残留：

```python
md = Markdown(extensions=["footnote"])
md.convert("warmup")
# 在渲染某个 FootnoteDef 时抛异常（自定义 renderer/monkeypatch 子渲染钩子）
md.convert("see[^x]\n\n[^x]: note\n")   # raises -> md.renderer.footnotes == ['x']
md.convert("see[^y]\n\n[^y]: note\n")   # KeyError: 'x'
```

实测第二次调用在 `marko/ext/footnote.py:108` 抛 `KeyError: 'x'`，同一 `Markdown` 实例不可
恢复。对称地，`render_footnote_ref` 用“标签在列表里则复用旧序号”的写法（`:76-78`），残留
状态还会让新文档脚注编号错位。注意基类 `Renderer.__exit__` 的恢复范围只有
`html._charref/root_node`（`marko/renderer.py:48-55`），覆盖不到扩展自带的累积列表。
本次按交付要求只修 A 并加一条测试；B 的最小修法是在 footnote mixin 的 `__enter__`
（或 `render_document` 的 try/finally）里重置 `self.footnotes`，与 Toc/LaTeX mixin 既有
做法一致（`marko/ext/toc.py:39-41`、`marko/ext/latex_renderer.py:21-27`）。

### 候选 C（行为已钉死，文档/注释错误）

两个后注册者胜出的环节都有实证（第 6 节）：mixin 前插使 MRO 后者靠前；elements 追加后
同 key 覆盖（`marko/__init__.py:89-102` + `marko/parser.py:54` +
`marko/element.py:133-147`）。`tests/test_basic.py:104-113` 已断言“gfm 之后注册的用户
mixin 胜出”，`tests/test_basic.py:87-92` 也断言了累积顺序。因此代码行为不可轻易改（改了
会破坏被钉住的扩展），真正需要修的只有 `marko/__init__.py:81` 这一处 docstring；正式文档
`docs/extend.rst:113-142` 的描述（“the last registered has the highest priority in the
MRO”，并给出 `C, B, A, HTMLRenderer` 的 MRO 示例）与实际行为一致。归类：**行为已被
测试钉住；docstring 与文档矛盾是已确认的小型文档缺陷**，建议单独文档 PR，不放入本次
补丁。

### 候选 D（分析后排除：无跨调用泄漏，仅单次解析异常安全缺口）

`Paragraph.parse` 的懒续行分支不用 `under_state`，而是
`states = source._states[:]` → 手工弹栈 → `source._states = states` 写回
（`marko/block.py:465-484`）。若 `break_paragraph`/`consume` 之间抛异常，写回被跳过。
但：(1) `Source` 在每次 `Parser.parse` 重新构造（`marko/parser.py:56-58`），`pos/anchor/
match/context/_states` 全部随对象销毁；(2) parser 自身在一次解析中不持有逐文档可变状态
（元素表是类注册表，`link_ref_defs/footnotes` 挂在本次新建的 Document 上）。所以该缺口
不会“串到下一次调用”，异常只会让当前 `parse()` 失败。`anchor/reset`、`match` 同理
（`marko/source.py:152-166`）。source map 方面，`_inline_positions` 在
`parse_inline` 完成后清空（`marko/parser.py:95-105`），span 全部按本次文本计算
（`marko/element.py:86-92`），复用同一 parser 连续解析多份文本不会互相污染位置（第 6 节
有连解析断言）。唯一跨解析的全局状态是
`Source.match_prefix` 的 `@functools.lru_cache(maxsize=128)`（`marko/source.py:94-111`），
它是**有界**且纯函数缓存，只带来固定上限的内存驻留，无正确性泄漏；若解析超长唯一行的
极长生命周期进程需要更严格内存控制，可考虑 `maxsize` 调小或按解析隔离，属于推测性优化。

## 6. 实证记录（均在 `.venv` 内执行）

- 链 2 输出：`> | a | b | …` 渲染为 `<blockquote>\n<table>…`，Quote→Table 结构与
  `source_span` 正确（table span 覆盖含 `>` 的三行，单元格 span 指向 `a/b/1/2`）；
  `1. | x | y | …`（表直接在 list item 内）实测解析为 List→ListItem→Paragraph。
- 链 3 输出：同一 AST 在 HTMLRenderer 下得到 `<sup class="footnote-ref">…` 加
  `<div class="footnotes">`；MarkdownRenderer 下回写 `see[^a] … [^a]: body text`；
  ASTRenderer 下元素名为 `footnote_ref/footnote_def`。
- 扩展顺序：`use(A,B)` 渲染为 B 的 `<p class=B>`、元素表 Paragraph 为 `ParaB`；
  `use(B,A)` 反之——后注册胜出。
- parser 复用：同一 `Markdown` 实例对不同文本连续 `parse/convert`，source span 与渲染
  均各自正确；候选 A 修复后异常 + 复用断言通过。

## 7. 交付物与验证

- 代码修复：`marko/html_renderer.py:108-118`（仅 try/finally 包裹恢复）。
- 最小证明测试：`tests/test_renderer_context.py:34-63`，先触发 alt 渲染异常，再复用同一
  `Markdown` 实例断言 `renderer.render` 已还原且第二次转换输出完整标签。
- 安装与演示：

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e . pytest
. .venv/bin/activate && pytest -q        # 1437 passed
```

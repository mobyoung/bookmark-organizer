# Bookmark Organizer（书签整理）

一个**端到端**的浏览器书签整理方案：把一团乱麻的收藏夹，变成「去重 + 清理失效 + 重新分类 + 加标签」的干净书签，再导回浏览器。

它最初是给 **WorkBuddy / CodeBuddy 这类 AI agent** 用的 Skill——**终端用户完全不需要懂命令行**，所有分析、分类、打标签的活儿都由 AI 完成，你只在几个关键节点做选择。同时，脚本本身也零依赖、可独立运行，方便开发者复用。

---

## 你（用户）需要做什么？

几乎不用做技术活，大致是：

1. 从浏览器**导出**书签（一个 HTML 文件），发给 AI。
2. 看 AI 给你的分析摘要（有多少书签、多少重复、多少可能打不开）。
3. 在几个问题上做选择：重复合并不合并？失效的删还是留？分类结构认不认可？
4. 拿到整理好的文件，**自己导入**浏览器（或让 AI 直接写进你的 Chrome）。

全程不用敲任何命令。

---

## 完整流水线

| 阶段 | 做什么 | 谁决定 |
| --- | --- | --- |
| **引导** | 若没给书签文件，AI 教你怎么从浏览器导出 | 你导出 |
| **分析** | 统计结构、找重复网址、探测可能失效的链接（候选） | AI 跑脚本 |
| **清理** | 合并重复、删除/暂存失效（不自动删，等你确认） | 你拍板 |
| **分类** | 把几十个杂乱文件夹归并成清晰的主题分类 | AI 提案，你确认 |
| **打标签** | 在每个分类内用 `【类型】`前缀标注内容性质 | AI 设计，你确认 |
| **交付** | 生成 HTML 给你自导入，或直写 Chrome | 你选方式 |

---

## 作为 AI Skill 使用（推荐）

把 `bookmark-organizer/` 整个目录放到：

- **项目级**：`<你的项目>/.workbuddy/skills/bookmark-organizer/`
- **用户级**：`~/.workbuddy/skills/bookmark-organizer/`

AI 会话中只要你说"整理书签""清理收藏夹""给书签分类"等，Skill 就会自动加载，按 `SKILL.md` 的流程带你走完。

---

## 脚本独立使用（开发者）

所有脚本**仅用 Python 标准库**，无需安装任何依赖：

```bash
# 1) 分析：结构 + 去重（离线，不联网）
python3 scripts/analyze_bookmarks.py --input bookmarks.html --mode summary \
    --json analysis.json --report analysis.txt

# 2)（可选）失效候选探测（联网；结果只作参考，以本地浏览器为准）
python3 scripts/analyze_bookmarks.py --input bookmarks.html --mode deadlinks \
    --limit 300 --json dead.json --report dead.txt

# 3) 清理：把你的决策写成 decisions.json，产出清理后 HTML + 改动清单
python3 scripts/clean_bookmarks.py --input bookmarks.html --decisions decisions.json \
    --output bookmarks_clean.html --changelog 书签改动清单.md

# 4) 打标签：按 taxonomy 给书签加 【类型】 前缀
python3 scripts/classify_bookmarks.py --input bookmarks_clean.html \
    --taxonomy assets/default_bookmark_taxonomy.json \
    --output bookmarks_tagged.html --report tag_report.txt

# 5) 导入：生成/直写 Chrome Bookmarks（多 profile 安全）
python3 scripts/build_chrome_bookmarks.py --original bookmarks.html \
    --classified bookmarks_tagged.html --profile "你的Profile"
```

> ⚠️ `build_chrome_bookmarks.py` 的 `--apply` 会**直接修改浏览器的 Bookmarks 文件**。普通用户请用方式 A（HTML 自导入），只在确需精确布局时让 AI 执行方式 B，且务必先完全退出 Chrome。

---

## 关于 Chrome 导入的真相（重要）

很多人以为"整理好结构后导入就该完美"，结果 Chrome 冒出一个 **「已导入」** 文件夹。这不是文件写错，是 **Chrome 导入器的固有行为**：

- 带 `PERSONAL_TOOLBAR_FOLDER` 标记书签栏 → 在"已有书签"的 profile 下，其余内容会被套进「已导入」容器。
- 不带该标记 → 无「已导入」，但所有内容直接铺进书签栏，"其他书签"为空。

**纯 HTML 导入无法同时满足「书签栏 N 条 + 其他书签 M 个分类 + 无已导入」**。要精确布局，只能直接写 Chrome 的 `Bookmarks` JSON（即方式 B）。详见 `references/chrome_import.md`。

---

## 自定义分类 / 标签

标签体系是一份 JSON（`assets/default_bookmark_taxonomy.json`），机器可读、可随意扩展：

- `folder_map`：顶层文件夹 → 默认 `【标签】`
- `subfolder_map`：子文件夹 → 更细 `【标签】`
- `keyword_refine`：按关键词细化
- `default_tag` / `root_tag`：兜底标签（根级书签，如书签栏的 `【快捷】`）

改完重新跑 `classify_bookmarks.py` 即可生效，不影响书签原有结构。用户**已有的**中括号前缀会原样保留。

---

## 目录结构

```
bookmark-organizer/
├── SKILL.md                      # AI agent 编排手册（完整流程）
├── README.md                     # 本文档
├── scripts/
│   ├── analyze_bookmarks.py      # 解析 / 去重 / 失效候选（离线优先）
│   ├── clean_bookmarks.py        # 应用清理决策，产出清理后 HTML + 改动清单
│   ├── classify_bookmarks.py     # 按 taxonomy 打 【类型】 前缀
│   └── build_chrome_bookmarks.py # 生成 / 直写 Chrome Bookmarks（profile 感知）
├── assets/
│   └── default_bookmark_taxonomy.json  # 默认标签体系模板
└── references/
    ├── intake_guide.md           # 各浏览器导出书签的非技术步骤
    ├── pipeline.md               # 方法论：去重规则 / 失效校验 / 分类原则 / 打标签
    └── chrome_import.md          # Chrome 导入行为深度说明
```

---

## License

MIT — 自由使用、修改、分发。

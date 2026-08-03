---
name: bookmark-organizer
description: 书签整理全流程。当用户想整理浏览器书签（去重、清理失效、重新分类、加标签、导回浏览器）时触发。覆盖：引导用户提供导出的书签文件 → 解析分析（去重/失效候选）→ 清理决策 → 分类整理提案 → AI 打标签（中括号前缀）→ 构建并导入/写入。终端用户无需懂命令行，所有脚本由 agent 在后台运行，agent 用大白话沟通并只在关键处征求确认。
agent_created: true
---

# 书签整理（Bookmark Organizer）

一个**端到端**的书签整理流水线，**由 AI agent 驱动、终端用户零命令行**。你（agent）负责所有分析、分类、打标签的智力工作；用户只需要在几个关键节点做选择/确认。你替用户跑全部脚本（用你的 Bash 工具），并把结果用人话讲给用户听。

## 何时触发
- 用户说"整理书签""清理收藏夹""书签太多了/乱了""去重""删失效书签""给书签分类打标签""导回浏览器"等。
- 用户丢给你一个浏览器导出的书签 HTML 文件，并希望整理它。

## 核心铁律
1. **用户不懂命令行**：绝不让用户自己敲 `python` 命令。所有 `scripts/*.py` 都由你用 Bash 调用。你只把结论和选项用自然语言告诉用户。
2. **不覆盖、不留黑箱**：永远生成新文件，保留原始导出。每步产生中间文件，最终交付可核查的清单。
3. **删除/移除是破坏操作，必须用户拍板**：你生成候选（重复、失效），但删不删、怎么删由用户决定。永不自动删除用户书签。
4. **用户本地视图是权威**：云端（你的运行环境）无法验证 NAS / 局域网 / 被墙站点，这类一律标"需本地确认"，不判失效。
5. **打标签由 AI 完成**：分类结构与 `【类型】`标签前缀由你（agent）基于书签内容设计，用户只确认方向，不要求用户手写规则。

---

## 阶段总览

| 阶段 | 你做什么 | 用户参与 | 产出 |
| --- | --- | --- | --- |
| **S0 引导** | 若用户没给文件，引导其从浏览器导出并发送 | 导出并发送 HTML | 书签 HTML |
| **S1 分析** | 跑 `analyze_bookmarks.py --mode summary`（离线）；可选 `deadlinks` | 看摘要，决定是否做失效探测 | 结构+去重报告 |
| **S2 清理** | 据用户决策生成 decisions.json，跑 `clean_bookmarks.py` | 决定重复/失效如何处理 | 清理后 HTML + 改动清单 |
| **S3 分类** | 设计目标顶层分类结构，与用户确认 | 确认/调整结构 | 分类方案 |
| **S4 打标签** | 设计 `【类型】`标签体系，跑 `classify_bookmarks.py` | 确认标签方向 | 带标签 HTML |
| **S5 交付** | 生成 HTML 给用户自导入，或直写 Chrome profile | 选交付方式 | 整理好的书签 |

---

## S0 · 引导用户提供书签文件

**如果用户已经在消息里给了 HTML 文件路径/附件** → 直接进入 S1。

**如果用户没给** → 用大白话引导导出（不要用术语轰炸）。示例话术：

> 我需要你浏览器里导出的书签文件（一个 HTML）。导出方法很简单：
> - **Chrome / Edge**：打开书签管理器（Chrome 按 `⌘+⇧+O` / Windows `Ctrl+⇧+O`）→ 右上角 ⋮ → **导出书签** → 会下载一个 `书签.html`。
> - **Firefox / Safari** 步骤类似（详见 `references/intake_guide.md`）。
> 把下载好的 HTML 发给我就行，剩下的整理都交给我。

把 `references/intake_guide.md` 当作各浏览器的详细步骤备查。收到文件后，记下它的路径，继续 S1。

> 顺便问一句（为 S5 做准备，但别现在就做）："你主要用哪个浏览器、哪个账户/profile 的书签？稍后导回时用得上。" 记下来即可。

---

## S1 · 分析（离线优先）

用**托管 Python**跑（不要污染用户环境）：
```
python3 scripts/analyze_bookmarks.py --input <用户文件> --mode summary \
    --json analysis.json --report analysis.txt
```
把 `analysis.txt` 的要点转述给用户，例如：

> 你的书签文件有 **1251 个书签、34 个顶层文件夹**。我发现 **4 组完全重复的网址**（同样链接存了多份），还有 **X 个本地/NAS 地址**我这边没法验证。接下来要不要我顺便帮你探一下哪些公网书签打不开？（这一步会联网、花点时间，结果只作参考，删除前你会再确认）

**是否做失效探测**：用 `AskUserQuestion` 问用户要不要跑 `deadlinks` 模式。若跑，建议后台运行（`run_in_background`），因为可能要几分钟：
```
python3 scripts/analyze_bookmarks.py --input <文件> --mode deadlinks \
    --limit 300 --json dead.json --report dead.txt
```
跑完后，把 `dead.txt` 的"疑似失效候选"清单**如实**交给用户，并强调：
> 这些是云端探出来的"疑似打不开"，但被墙/内网/反爬会误判。**最终删不删，以你本地浏览器能打开为准**，你圈出要删的我来执行。

---

## S2 · 清理（用户决策后机械执行）

基于 S1 的候选，向用户确认处理方式（用 `AskUserQuestion` 或列选项）：
- **重复**：建议合并（每组留一份）。可问"全部合并 / 我来指定留哪份"。
- **失效候选**：逐批问"删除 / 移入『待复查』文件夹 / 保留"。

你（agent）把用户的决定写成 `decisions.json`：
```json
{
  "merge_groups": [ ["https://x.com/a", "https://x.com/a"] ],
  "dead_actions": {
    "https://dead.example": "delete",
    "https://maybe.example": "move_to_review"
  }
}
```
然后跑：
```
python3 scripts/clean_bookmarks.py --input <原文件> --decisions decisions.json \
    --output bookmarks_clean.html --changelog 书签改动清单.md
```
把 `书签改动清单.md` 交付给用户——这是透明的审计轨迹，每一条删除/合并都写明了 URL 和依据。

---

## S3 · 分类整理（你设计结构，用户确认）

读 `bookmarks_clean.html`（或分析阶段的结构），**你**提出目标顶层分类方案。原则（详见 `references/pipeline.md`）：
- 按用户的身份主线（如设计师/开发者/AI 爱好者）划分顶层，**不**按字母/时间/来源。
- 合并同名中英文夹（如 `摄影`/`Photography`）、拆分大杂烩（如"资源"拆成设计资源/学习资料/软件工具/影音资源）。
- 本地/企业收藏夹（NAS、服务器、公司内网）保持独立不合并。
- 用户原有的子文件夹结构尽量保留，只做必要的归并。

用一张表呈现「现有顶层 → 目标顶层」映射，请用户确认或调整。必要时用 `AskUserQuestion` 处理分歧点（例如"設計综合要不要拆出子学科"）。

> 这一步**不一定要脚本**——分类重组可由你在 S4 的 taxonomy 里体现，再由 `classify_bookmarks.py` 应用；或生成一个新的结构化 HTML。优先用 taxonomy 驱动，保持可复现。

---

## S4 · 打标签（AI 决定标签体系，脚本应用）

这是用户明确要求的"**打标签由 AI 完成**"。做法：

1. **你设计标签**：在每个顶层分类下，决定用哪些 `【类型】`前缀标注内容性质，例如：
   - 设计 → `【UI】 【前端】 【品牌】 【素材】 【字体】 【灵感】 …`
   - 人工智能 → `【AI-大模型】 【AI-文生图】 【AI-Agent】 【AI教程】 …`
   - 资讯 → `【新闻-科技】 【新闻-财经】 【娱乐】 …`
   标签应**贴合该用户书签的实际内容**，不要生搬硬套。
2. 把标签体系写进一份 taxonomy JSON（可基于 `assets/default_bookmark_taxonomy.json` 改写），包含：
   - `folder_map`：顶层文件夹 → 默认标签
   - `subfolder_map`：子文件夹 → 更细标签
   - `keyword_refine`：按关键词细化
   - `default_tag` / `root_tag`（根级书签，如书签栏的 `【快捷】`）
3. 用户已有前缀**原样保留**（不重写）。
4. 跑引擎：
   ```
   python3 scripts/classify_bookmarks.py --input bookmarks_clean.html \
       --taxonomy taxonomy.json --output bookmarks_tagged.html --report tag_report.txt
   ```
5. 看 `tag_report.txt`：重点检查归入 `【其他】` 和 `【快捷】` 的书签，挑出明显贴错/该细分的，回头补 `keyword_refine` 后重跑，直到覆盖率满意。

---

## S5 · 交付与导入（让用户选方式）

向用户说明两种方式，**用 `AskUserQuestion` 让其选择**：

**方式 A — HTML 自导入（最透明、最安全）**
- 你生成 `bookmarks_tagged.html` 交付。
- 用户自己：书签管理器 → ⋮ → 导入书签 → 选该文件。
- ⚠️ **诚实告知 Chrome 行为**（详见 `references/chrome_import.md`）：
  - 若文件带 `PERSONAL_TOOLBAR_FOLDER` 标记书签栏，Chrome 在"已有书签"的 profile 下会把其余内容套进「已导入」文件夹（需手动拖一次到"其他书签"）。
  - 不带该标记 → 无「已导入」，但所有内容直接铺进书签栏，"其他书签"为空。
  - **纯 HTML 无法同时满足"书签栏 N + 其他书签 M + 无已导入"**。这是 Chrome 导入器的硬限制，不是文件写错。

**方式 B — 直写 Chrome profile（精确布局、零手动）**
- 仅当布局要求严格（书签栏 N + 其他书签 M + 无「已导入」）时推荐。
- 你跑 `scripts/build_chrome_bookmarks.py`（profile 感知）：
  ```
  python3 scripts/build_chrome_bookmarks.py --original <用户原始导出> \
      --classified bookmarks_tagged.html --profile "Profile 名或编号"
  ```
  - 不指定 `--profile` 且检测到多个 profile 时，脚本会列出并让你用 `--profile` 指定（**绝不静默写错**）。
  - 加 `--apply` 才真正写入（会先备份原 `Bookmarks` 到 `chrome_bookmarks_backups/`，并校验 Chrome 已完全退出）。
- 前置：用户需**完全退出 Chrome**（⌘Q），并建议先关同步防止云端覆盖。写完用户再开 Chrome 即可看到结果。
- ⚠️ 这是直接改用户浏览器数据文件——必须在用户明确选择 B 且理解了备份/退出要求后再执行。

交付后，给用户一份"你做了什么"的简短总结 + 关键文件清单，并提示可随时回退（有备份 / 有原始导出）。

---

## 脚本速查（你调用，用户不调用）

| 脚本 | 用途 | 是否改文件 |
| --- | --- | --- |
| `scripts/analyze_bookmarks.py` | 结构+去重（summary）/ 失效候选（deadlinks） | 否（只读） |
| `scripts/clean_bookmarks.py` | 应用 decisions.json，产出清理后 HTML + 改动清单 | 产出新文件 |
| `scripts/classify_bookmarks.py` | 按 taxonomy 打 `【标签】`前缀 | 产出新文件 |
| `scripts/build_chrome_bookmarks.py` | 生成/直写 Chrome Bookmarks（profile 感知） | `--apply` 时才写 |

所有脚本**零第三方依赖**，用托管 Python 即可（`python3 scripts/xxx.py`）。

## 避坑提示
- **别让"已导入"背锅**：它不是你文件写错，是 Chrome 导入器对 `PERSONAL_TOOLBAR_FOLDER` 的固有行为。需要精确布局就走方式 B。
- **失效探测不可靠**：被墙/内网/反爬站点会误判，永远以用户本地为准，只生成候选。
- **多 profile 别写错**：方式 B 一定要让用户指定目标 profile，脚本不写死路径。
- **保留原始文件**：任何阶段都不覆盖用户给的 HTML；所有产物是新文件名。

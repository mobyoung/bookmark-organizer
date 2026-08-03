# Chrome 书签 HTML 格式与导入行为（深度说明）

本文档沉淀自一次完整的书签整理实战，记录了 Chrome 书签 HTML 导入的**真实行为边界**。
这些结论都是「改了又错、错再改」试出来的，不是想当然；适用于任何想把书签**精确导入**
Chrome（书签栏 N 条 + 其他书签 M 分类 + 无「已导入」）的人。

## 1. Netscape 书签 HTML 格式要点

Chrome 导出的书签 HTML 结构（节选）：

```html
<!DOCTYPE NETSCAPE-Bookmark-file-1>
<H1>书签</H1>
<DL><p>
    <H3 PERSONAL_TOOLBAR_FOLDER="true">书签栏</H3>
    <DL><p>
        <DT><A HREF="https://example.com" ADD_DATE="1690000000">便利书签1</A>
        ...
    </DL><p>
    <H3>設計综合</H3>
    <DL><p>
        <DT><A HREF="...">某网站</A>
        ...
    </DL><p>
    ...
</DL><p>
```

关键规律（**对照 Chrome 自己导出的文件验证**）：
- **书签栏** = 顶层 `<H3>` 带 `PERSONAL_TOOLBAR_FOLDER="true"`，导入后该层被**剥离**，
  内部书签直接进书签栏。
- **其他书签里的内容**（如各分类文件夹）在 HTML 里就是**普通顶层 `<H3>` 文件夹**，
  不带任何特殊属性。Chrome 导入自己导出的这种格式时，这些顶层文件夹应自动落入系统「其他书签」。
- 嵌套层级用 `<DL><p>` ... `</DL><p>` 表达，与 `<H3>` / `<A>` 交替出现。

## 2. Chrome「导入书签」的两条死路（实测定论）

用 HTML 导入，**无法**同时满足「书签栏 N 条 + 其他书签 M 分类 + 无已导入 + 零操作」：

| 方案 | HTML 写法 | 导入结果 | 结论 |
| --- | --- | --- | --- |
| 带属性 | 书签栏带 `PERSONAL_TOOLBAR_FOLDER="true"`，分类作普通顶层文件夹 | 书签栏提升 N 条；**必出现「已导入」容器**，分类留在容器内 | ❌ 有「已导入」 |
| 不带属性 | N 条 + M 分类全部作根级（无标记） | **无「已导入」**，但全部铺进书签栏，「其他书签」永远空 | ❌ 层级错 |

**关键修正（推翻早前假设）**：早前曾以为「导入前清空 Chrome 就不会有已导入」。但在
**全新 profile、已清空**的状态下实测：带属性的文件**仍然**产生「已导入」。说明「已导入」的触发
器就是 `PERSONAL_TOOLBAR_FOLDER` 这个属性本身，与 profile 是否为空、是否新建、是否关同步无关。

## 3. 为什么「已导入」绕不开（HTML 路线）

「已导入」是 Chrome 导入器在检测到「完整书签导出（带书签栏标记）+ 目标已有书签结构」时，
为保护现有书签而套的临时容器。HTML 结构再正确也绕不开：
- 带属性 → 被识别为「完整导出」→ 建「已导入」保护容器；
- 不带属性 → 被当成「一堆书签」直接铺进书签栏 → 无容器，但层级丢失。

## 4. 唯一干净的路：直接写 Bookmarks JSON

Chrome 真正存储书签的是 `Bookmarks` JSON 文件，`roots` 下有：
- `bookmark_bar`（id=1）：书签栏
- `other`（id=2）：其他书签
- `synced`（id=3）：移动设备书签

直接构造这个 JSON、只替换三个根的 `children`，即可**精确控制布局、无「已导入」、零操作**。
这正是 `scripts/build_chrome_bookmarks.py` 做的事。

### 写入安全清单（必须遵守）
1. **确认目标 profile 的 Chrome 已完全退出**：检查 `<profile>/SingletonLock` 不存在。
   （用户退出 Chrome 时内存状态会写回磁盘，覆盖你的修改——这是最易翻车的点。）
2. **自动备份**：写入前 `shutil.copy2` 原文件到 `chrome_bookmarks_backups/`，出问题可还原。
3. **多 profile 处理**：绝不要写死 `Default`；自动发现所有 profile，单 profile 直用、
   多 profile 让用户选（见 SKILL.md）。
4. **保留元数据**：沿用现有 `roots` 的 `id/guid/name`，只换 `children`；写后清 `checksum`
   让 Chrome 启动时重算。
5. **时间字段**：Chrome 用 `(unix_sec + 11644473600) * 1_000_000` 微秒表示时间。
6. **id 不冲突**：新节点 id 取「现有最大 id + 1」递增；`guid` 用 `uuid4()`。

## 5. 各平台 Chrome 书签路径

- macOS：`~/Library/Application Support/Google/Chrome/<Profile>/Bookmarks`
- Windows：`%LOCALAPPDATA%/Google/Chrome/<Profile>/Bookmarks`
- Linux：`~/.config/google-chrome/<Profile>/Bookmarks`

`<Profile>` 通常是 `Default`、`Profile 1`、`Profile 2`…（「新建 profile」会顺序编号）。

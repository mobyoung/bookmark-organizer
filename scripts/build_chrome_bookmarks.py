#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_chrome_bookmarks.py — Chrome 书签 JSON 直写器（profile 感知，可复用）

为什么需要它：
  Chrome 导入 HTML 时，带 PERSONAL_TOOLBAR_FOLDER 属性必产生「已导入」容器，
  不带则全部进书签栏、「其他书签」永远空——两条路都无法同时满足
  「书签栏 N 条 + 其他书签 M 分类 + 无已导入」。直接写 Chrome 的 Bookmarks JSON
  可绕过导入器，精确控制 bookmark_bar / other 两个根节点的 children。

profile 安全（重点）：
  - 自动发现本机所有 Chrome profile；
  - 只有 1 个 profile → 直接用它，无需选择；
  - 多个 profile → 运行时弹出编号菜单让用户自己选（STDIN 非交互时改用 --profile 指定）；
  - 只检查「目标 profile」自己的 SingletonLock，不会误判其它 profile 的运行状态，
    也绝不会写错 profile。

输入：
  --original   用户 Chrome 原始导出 HTML（取「书签栏」里的便利书签）
  --classified 已打【】前缀的分类版 HTML（取根级分类文件夹）

输出：
  （默认）生成预览 JSON（bookmarks_Chrome_preview.json）并打印统计，不写任何文件
  --apply      在「目标 profile 完全退出」时，备份后写入该 profile 的 Bookmarks 文件
               （保留 roots 的 id/guid/name 等元数据，只替换 children）
"""
import argparse
import json
import os
import shutil
import sys
import time
import uuid
import glob
from html.parser import HTMLParser

BACKUP_DIR = "chrome_bookmarks_backups"
EPOCH_OFFSET = 11644473600  # Chrome 时间 = (unix_sec + offset) * 1e6 微秒

# 不同系统的 Chrome 根目录
def chrome_root():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    if sys.platform == "win32":
        return os.path.expanduser(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google/Chrome"))
    return os.path.expanduser("~/.config/google-chrome")


class BookmarkParser(HTMLParser):
    """把 Netscape Bookmark HTML 解析成 {type,name,children,url,add_date} 树。"""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"type": "folder", "name": "ROOT", "children": []}
        self.stack = [self.root]
        self._in_h3 = False
        self._h3_name = ""
        self._h3_attrs = {}
        self._cur_url = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "h3":
            self._in_h3 = True
            self._h3_name = ""
            self._h3_attrs = a
        elif tag == "a":
            self._cur_url = {"type": "url", "url": a.get("href", ""),
                             "name": "", "add_date": a.get("add_date", "")}

    def handle_endtag(self, tag):
        if tag == "h3":
            self._in_h3 = False
            folder = {"type": "folder", "name": self._h3_name.strip(),
                      "children": [], "add_date": self._h3_attrs.get("add_date", "")}
            self.stack[-1]["children"].append(folder)
            self.stack.append(folder)
        elif tag == "a":
            if self._cur_url is not None:
                self.stack[-1]["children"].append(self._cur_url)
                self._cur_url = None
        elif tag == "dl":
            if len(self.stack) > 1:
                self.stack.pop()

    def handle_data(self, data):
        if self._in_h3:
            self._h3_name += data
        elif self._cur_url is not None:
            self._cur_url["name"] += data


def parse_html(path):
    p = BookmarkParser()
    p.feed(open(path, encoding="utf-8").read())
    p.close()
    return p.root


def chrome_time(add_date):
    try:
        sec = int(add_date)
    except (ValueError, TypeError):
        sec = 0
    return str(int((sec + EPOCH_OFFSET) * 1000000))


_id_counter = [0]

def next_id(existing_max):
    _id_counter[0] = max(_id_counter[0], existing_max) + 1
    return str(_id_counter[0])


def to_chrome(node, existing_max):
    if node["type"] == "url":
        return {"type": "url", "id": next_id(existing_max), "url": node["url"],
                "name": node["name"].strip(), "date_added": chrome_time(node.get("add_date")),
                "date_last_used": "0", "guid": str(uuid.uuid4())}
    return {"type": "folder", "id": next_id(existing_max), "name": node["name"].strip(),
            "date_added": chrome_time(node.get("add_date")), "date_last_used": "0",
            "date_modified": "0", "guid": str(uuid.uuid4()),
            "children": [to_chrome(c, existing_max) for c in node["children"]]}


def find_max_id(node):
    nid = node.get("id", "0")
    m = int(nid) if str(nid).isdigit() else 0
    for c in node.get("children", []):
        m = max(m, find_max_id(c))
    return m


def build_bookmark_nodes(original_path, classified_path):
    """返回 (bar_nodes, other_nodes)：书签栏便利书签 + 其他书签分类文件夹。"""
    orig = parse_html(original_path)
    clas = parse_html(classified_path)
    bar_folder = None
    for c in orig["children"]:
        if c["type"] == "folder" and c["name"] == "书签栏":
            bar_folder = c
            break
    if not bar_folder:
        for c in orig["children"]:
            if c["type"] == "folder":
                bar_folder = c
                break
    bar_urls = [c for c in bar_folder["children"] if c["type"] == "url"]
    other_folders = [c for c in clas["children"] if c["type"] == "folder"]
    return bar_urls, other_folders


def discover_profiles(root):
    """返回本机所有 Chrome profile 目录信息。"""
    profiles = []
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        if not os.path.isdir(d):
            continue
        name = os.path.basename(d)
        bm = os.path.join(d, "Bookmarks")
        pref = os.path.join(d, "Preferences")
        if not (os.path.exists(bm) or os.path.exists(pref)):
            continue
        cnt = 0
        if os.path.exists(bm):
            try:
                dd = json.load(open(bm, encoding="utf-8"))
                def cc(nodes):
                    n = 0
                    for n_ in nodes:
                        if n_.get("type") == "url":
                            n += 1
                        else:
                            n += cc(n_.get("children", []))
                    return n
                cnt = cc(dd.get("roots", {}).get("bookmark_bar", {}).get("children", [])) + \
                      cc(dd.get("roots", {}).get("other", {}).get("children", []))
            except Exception:
                cnt = -1
        profiles.append({"name": name, "dir": d, "count": cnt})
    return profiles


def count_bookmarks(nodes):
    n = 0
    for x in nodes:
        if x.get("type") == "url":
            n += 1
        else:
            n += count_bookmarks(x.get("children", []))
    return n


def build_preview(bar_nodes, other_nodes):
    return {
        "checksum": "",
        "roots": {
            "bookmark_bar": {"type": "folder", "id": "1", "name": "书签栏",
                             "date_added": "0", "date_last_used": "0", "date_modified": "0",
                             "guid": "00000000-0000-4000-a000-000000000001", "children": bar_nodes},
            "other": {"type": "folder", "id": "2", "name": "其他书签",
                      "date_added": "0", "date_last_used": "0", "date_modified": "0",
                      "guid": "00000000-0000-4000-a000-000000000002", "children": other_nodes},
            "synced": {"type": "folder", "id": "3", "name": "移动设备书签",
                       "date_added": "0", "date_last_used": "0", "date_modified": "0",
                       "guid": "00000000-0000-4000-a000-000000000003", "children": []},
        },
        "version": 5,
    }


def pick_profile(profiles, profile_arg):
    """按规则选择目标 profile：单 profile 直用；多 profile 交互或 --profile 指定。"""
    if profile_arg:
        names = [p["name"] for p in profiles]
        if profile_arg not in names:
            print(f"⚠️ 找不到 profile「{profile_arg}」。可用：{names}")
            sys.exit(1)
        return next(p for p in profiles if p["name"] == profile_arg)

    if len(profiles) == 1:
        return profiles[0]

    # 多个 profile
    if not sys.stdin.isatty():
        print("检测到多个 Chrome profile，但当前为非交互环境。请用 --profile 指定，例如：")
        for p in profiles:
            print(f"  --profile \"{p['name']}\"   ({p['dir']})")
        sys.exit(1)

    print("\n检测到多个 Chrome profile，请选择目标：")
    for i, p in enumerate(profiles, 1):
        cnt = p["count"]
        cnt_s = f"{cnt} 书签" if cnt >= 0 else "无法读取"
        print(f"  [{i}] {p['name']:<16} {cnt_s}")
    while True:
        try:
            choice = input("请输入编号: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            sys.exit(1)
        if choice.isdigit() and 1 <= int(choice) <= len(profiles):
            return profiles[int(choice) - 1]
        print("  无效输入，请重新输入编号。")


def main():
    ap = argparse.ArgumentParser(description="Chrome 书签 JSON 直写器（profile 感知）")
    ap.add_argument("--original", default="bookmarks.html", help="原始导出 HTML（取书签栏便利书签）")
    ap.add_argument("--classified", default="bookmarks_classified.html", help="已分类标记 HTML（取根级分类）")
    ap.add_argument("--preview-out", default="bookmarks_preview.json", help="预览 JSON 输出路径")
    ap.add_argument("--profile", default=None, help="指定目标 profile 目录名（多 profile 时）")
    ap.add_argument("--list-profiles", action="store_true", help="仅列出本机 Chrome profile")
    ap.add_argument("--apply", action="store_true", help="写入目标 profile 的 Bookmarks（需该 profile 完全退出）")
    args = ap.parse_args()

    root = chrome_root()
    profiles = discover_profiles(root)

    if args.list_profiles:
        print("本机 Chrome profile：")
        for p in profiles:
            cnt = p["count"]
            cnt_s = f"{cnt} 书签" if cnt >= 0 else "无法读取"
            print(f"  - {p['name']:<16} {cnt_s:<12} {p['dir']}")
        return

    if not profiles:
        print(f"⚠️ 在 {root} 下未发现任何 Chrome profile。")
        sys.exit(1)

    target = pick_profile(profiles, args.profile)
    CHROME_BOOKMARKS = os.path.join(target["dir"], "Bookmarks")
    SINGLETON_LOCK = os.path.join(target["dir"], "SingletonLock")
    print(f"目标 profile：{target['name']}  →  {CHROME_BOOKMARKS}")

    if not os.path.exists(args.original) or not os.path.exists(args.classified):
        print(f"⚠️ 找不到输入文件：{args.original} / {args.classified}")
        sys.exit(1)

    bar_urls, other_folders = build_bookmark_nodes(args.original, args.classified)
    print(f"书签栏便利书签: {len(bar_urls)} 条")
    print(f"其他书签分类: {len(other_folders)} 个")

    existing_max = 0
    if os.path.exists(CHROME_BOOKMARKS):
        try:
            ex = json.load(open(CHROME_BOOKMARKS, encoding="utf-8"))
            for k in ("bookmark_bar", "other", "synced"):
                existing_max = max(existing_max, find_max_id(ex["roots"][k]))
        except Exception as e:
            print(f"(读取现有 Bookmarks 取 max id 失败: {e}，从 0 开始)")

    bar_nodes = [to_chrome(u, existing_max) for u in bar_urls]
    other_nodes = [to_chrome(f, existing_max) for f in other_folders]

    preview = build_preview(bar_nodes, other_nodes)
    json.dump(preview, open(args.preview_out, "w", encoding="utf-8"), ensure_ascii=False, indent=3)
    print(f"预览已写入 {args.preview_out}（书签栏 {len(bar_nodes)} + 其他书签 {len(other_nodes)} 分类）")

    if not args.apply:
        print("（加 --apply 写入 Chrome，需目标 profile 完全退出）")
        return

    if os.path.exists(SINGLETON_LOCK):
        print(f"⚠️ 目标 profile「{target['name']}」似乎在运行（SingletonLock 存在），请先完全退出该 profile 的 Chrome 再 --apply。")
        sys.exit(1)
    if not os.path.exists(CHROME_BOOKMARKS):
        print(f"⚠️ 未找到 {CHROME_BOOKMARKS}")
        sys.exit(1)

    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(BACKUP_DIR, f"Bookmarks.backup.{target['name']}.{ts}.json")
    shutil.copy2(CHROME_BOOKMARKS, bak)
    print(f"已备份原 Bookmarks → {bak}")

    ex = json.load(open(CHROME_BOOKMARKS, encoding="utf-8"))
    for key, nodes in (("bookmark_bar", bar_nodes), ("other", other_nodes), ("synced", [])):
        ex["roots"][key]["children"] = nodes
    ex["checksum"] = ""  # 让 Chrome 启动时重算
    json.dump(ex, open(CHROME_BOOKMARKS, "w", encoding="utf-8"), ensure_ascii=False, indent=3)
    print(f"✅ 已写入 profile「{target['name']}」：书签栏 {len(bar_nodes)} 条 + 其他书签 {len(other_nodes)} 分类，无已导入。")
    print("打开该 profile 的 Chrome 即可看到结果。")


if __name__ == "__main__":
    main()

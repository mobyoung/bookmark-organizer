#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_bookmarks.py — 书签解析 / 去重 / 失效候选检测（无第三方依赖）

这是「书签整理」流水线的第 1 步（分析）。它**只读取、不修改**书签文件，产出：
  - 结构摘要：书签总数、顶层文件夹及数量、层级概览
  - 去重分组：归一化 URL 精确匹配的重复书签（相同网址只应保留一份）
  - 失效候选（可选、走网络）：对公网 URL 做可达性探测，结果仅为「候选」，
    需用户在本地浏览器最终确认（云端无法验证 NAS / 局域网 / 被墙站点）

设计原则
--------
- 默认 --mode summary：**完全离线**，只做结构 + 去重，不碰网络。
- --mode deadlinks：对公网 URL 做 HTTP 探测。本地/内网/浏览器内部协议一律跳过，
  并明确标注「需本地确认」。探测结果不自动删除任何书签。
- 所有判定都是「候选」，最终删除/保留由用户决定，agent 负责把候选呈现给用户。

用法（由 AI agent 在后台调用，终端用户不碰命令行）
-------------------------------------------------
  # 结构 + 去重（离线，默认）
  python3 analyze_bookmarks.py --input bookmarks.html --mode summary \
      [--json report.json] [--report report.txt]

  # 失效候选探测（走网络，建议后台运行）
  python3 analyze_bookmarks.py --input bookmarks.html --mode deadlinks \
      [--limit 200] [--json dead.json] [--report dead.txt]

输出
----
  - stdout 打印人类可读摘要（agent 可直接转述给用户）
  - --json 写出机器可读结果（供 agent 下一步生成决策/调用 clean_bookmarks.py）
"""
import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser


# ============================================================ 解析器
class BookmarkParser(HTMLParser):
    """提取每一条书签的 href / 名称 / 文件夹路径。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.folder_stack = []
        self.pending = None
        self.in_a = False
        self.a_text = ""
        self.a_href = None
        self.a_path = None
        self.bookmarks = []  # dict(href, name, path)

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "h3":
            self.pending = ""
        elif tag == "dl":
            if self.pending is not None:
                self.folder_stack.append(self.pending.strip())
                self.pending = None
        elif tag == "a" and "href" in d:
            self.in_a = True
            self.a_text = ""
            self.a_href = d["href"]
            self.a_path = list(self.folder_stack)

    def handle_endtag(self, tag):
        if tag == "dl" and self.pending is None and self.folder_stack:
            self.folder_stack.pop()
        elif tag == "a" and self.in_a:
            self.bookmarks.append({
                "href": self.a_href,
                "name": self.a_text.strip(),
                "path": self.a_path,
            })
            self.in_a = False

    def handle_data(self, data):
        if self.pending is not None:
            self.pending += data
        elif self.in_a:
            self.a_text += data


# ============================================================ URL 工具
def unescape_url(u: str) -> str:
    return u.replace("&amp;", "&").replace("&#38;", "&").replace("&lt;", "<").replace("&gt;", ">")


def normalize_url(u: str) -> str:
    """去重归一化：小写 scheme/host、去默认端口、去 userinfo、去 fragment、去末尾多余斜杠。"""
    u = unescape_url((u or "").strip())
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://([^/]+)(.*)$", u)
    if not m:
        return u.lower()  # 非标准 URL（如 scheme-relative），仅小写用于比较
    scheme, host, rest = m.groups()
    scheme = scheme.lower()
    host = host.lower()
    if "@" in host:  # 去掉 user:pass@
        host = host.split("@", 1)[1]
    if ":" in host:
        h, p = host.rsplit(":", 1)
        if (scheme == "http" and p == "80") or (scheme == "https" and p == "443"):
            host = h
    rest = rest.split("#", 1)[0]  # 去掉锚点
    if len(rest) > 1 and rest.endswith("/"):
        rest = rest.rstrip("/")  # 去掉末尾斜杠（保留路径内部斜杠）
    return f"{scheme}://{host}{rest}"


# 本地 / 内网 / 浏览器内部协议：云端无法验证，留待本地确认
PRIVATE_HOST_FRAGMENTS = ("localhost", "127.0.0.1", "::1", "192.168.", "10.",
                          "169.254.", "172.16.", "172.17.", "172.18.", "172.19.",
                          "172.2", "172.30.", "172.31.", "100.", ".local", "127.")
INTERNAL_SCHEMES = ("file:", "chrome:", "about:", "edge:", "brave:", "opera:",
                    "vivaldi:", "moz-extension:", "extension:", "view-source:",
                    "chrome-extension:", "data:")


def is_internal(url: str) -> bool:
    u = (url or "").lower()
    if any(u.startswith(p) for p in INTERNAL_SCHEMES):
        return True
    if any(frag in u for frag in PRIVATE_HOST_FRAGMENTS):
        return True
    return False


def host_of(url: str) -> str:
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^/]+)", url)
    if not m:
        return ""
    h = m.group(1).lower()
    if "@" in h:
        h = h.split("@", 1)[1]
    if ":" in h:
        h = h.rsplit(":", 1)[0]
    return h


# ============================================================ 结构摘要
def build_tree(bookmarks):
    """返回 (顶层统计, 全文件夹统计)。"""
    top = Counter()
    folders = Counter()
    for b in bookmarks:
        path = b["path"] or ["(根级)"]
        top[path[0]] += 1
        # 累加每一层
        for i in range(1, len(path) + 1):
            folders["/".join(path[:i])] += 1
    return top, folders


def analyze_summary(bookmarks):
    total = len(bookmarks)
    top, folders = build_tree(bookmarks)

    # 去重：按归一化 URL 分组
    groups = defaultdict(list)
    for b in bookmarks:
        key = normalize_url(b["href"])
        groups[key].append(b)
    dup_keys = {k: v for k, v in groups.items() if len(v) > 1}
    dup_pairs = sum(len(v) - 1 for v in dup_keys.values())  # 可移除的重复条数

    # 内部/本地地址数量（无法云端验证失效）
    internal_count = sum(1 for b in bookmarks if is_internal(b["href"]))

    return {
        "total": total,
        "top_folders": dict(top.most_common()),
        "top_folder_count": len(top),
        "dup_groups": len(dup_keys),
        "dup_removable": dup_pairs,
        "internal_count": internal_count,
        "dup_detail": [
            {
                "url": k,
                "count": len(v),
                "items": [{"name": x["name"], "path": x["path"]} for x in v],
            }
            for k, v in sorted(dup_keys.items(), key=lambda kv: -len(kv[1]))
        ],
    }


# ============================================================ 失效候选探测
def build_variants(url: str):
    """探测变体：原样、http<->https、带/不带 www、站点主页。"""
    variants = []
    scheme = "https" if url.lower().startswith("http") else None
    if scheme is None:
        return variants
    host = host_of(url)
    if not host:
        return variants
    path = url.split(host, 1)[1] if host in url else ""
    base = f"{scheme}://{host}"
    # 原样（仅当是 https 优先；http 则在最后补）
    if url.lower().startswith("https"):
        variants.append(url)
    # homepage
    variants.append(base)
    # www 变体
    if host.startswith("www."):
        variants.append(f"{scheme}://{host[4:]}")
        variants.append(f"{scheme}://{host[4:]}{path}")
    else:
        variants.append(f"{scheme}://www.{host}")
        variants.append(f"{scheme}://www.{host}{path}")
    # http 版本
    if url.lower().startswith("https"):
        variants.append("http://" + url[len("https://"):])
    variants = list(dict.fromkeys(variants))  # 去重保序
    return variants


def probe(url: str):
    """返回 (status, detail)。status ∈ alive/dead/error。"""
    for cand in build_variants(url):
        try:
            req = urllib.request.Request(
                cand, method="GET",
                headers={"User-Agent": "Mozilla/5.0 (bookmark-checker)"},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                if r.status < 400:
                    return "alive", cand
                # 4xx/5xx → 试下一个变体
        except urllib.error.HTTPError as e:
            if e.code < 400:
                return "alive", cand
            # 继续尝试其他变体
        except Exception:
            continue
    return "dead", None


def analyze_deadlinks(bookmarks, limit=None, workers=10):
    public = [b for b in bookmarks if not is_internal(b["href"])]
    if limit:
        public = public[:limit]
    results = []
    checked = 0
    dead = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(probe, b["href"]): b for b in public}
        for fut in as_completed(futs):
            b = futs[fut]
            checked += 1
            try:
                status, detail = fut.result()
            except Exception as e:  # noqa
                status, detail = "error", str(e)
            if status == "dead":
                dead += 1
                results.append({
                    "name": b["name"], "href": b["href"],
                    "path": b["path"], "status": "dead",
                })
    return {
        "checked_public": checked,
        "skipped_internal": len(bookmarks) - len(public),
        "dead_candidates": dead,
        "dead_list": results,
    }


# ============================================================ 报告
def summary_text(s):
    L = []
    L.append("=" * 60)
    L.append("书签分析 · 结构摘要（离线）")
    L.append("=" * 60)
    L.append(f"书签总数          : {s['total']}")
    L.append(f"顶层文件夹数      : {s['top_folder_count']}")
    L.append(f"内部/本地地址数   : {s['internal_count']}（云端无法验证，需本地确认）")
    L.append(f"重复分组（URL相同）: {s['dup_groups']} 组，可移除重复 {s['dup_removable']} 条")
    L.append("")
    L.append("--- 顶层文件夹（按数量）---")
    for name, c in s["top_folders"].items():
        L.append(f"  {c:4d}  {name}")
    if s["dup_detail"]:
        L.append("")
        L.append(f"--- 重复分组明细（前 20）---")
        for g in s["dup_detail"][:20]:
            L.append(f"  [{g['count']}份] {g['url']}")
            for it in g["items"][:3]:
                L.append(f"        · {it['name'][:30]}  （{'/'.join(it['path']) or '根级'}）")
    return "\n".join(L)


def deadlink_text(d):
    L = []
    L.append("=" * 60)
    L.append("失效候选探测（走网络 · 仅为候选，需本地最终确认）")
    L.append("=" * 60)
    L.append(f"已探测公网 URL    : {d['checked_public']}")
    L.append(f"跳过内部/本地地址 : {d['skipped_internal']}（NAS/局域网/被墙站点无法从云端验证）")
    L.append(f"疑似失效候选      : {d['dead_candidates']}")
    L.append("")
    L.append("--- 疑似失效（前 40，请用户在本地浏览器核对）---")
    for it in d["dead_list"][:40]:
        L.append(f"  {it['name'][:34]:34s} <- {it['href']}")
    L.append("")
    L.append("注意：云端探测不可靠（被墙/内网/反爬会误判）。")
    L.append("      这些只是候选，删除前请用户在本机确认。")
    return "\n".join(L)


# ============================================================ 主流程
def main():
    ap = argparse.ArgumentParser(description="书签分析：结构/去重/失效候选")
    ap.add_argument("--input", required=True, help="书签 HTML 文件")
    ap.add_argument("--mode", choices=["summary", "deadlinks"], default="summary",
                    help="summary=结构+去重(离线,默认)；deadlinks=额外做失效探测(走网络)")
    ap.add_argument("--limit", type=int, default=None, help="deadlinks 模式最多探测的 URL 数")
    ap.add_argument("--json", default=None, help="机器可读结果输出路径")
    ap.add_argument("--report", default=None, help="人类可读报告输出路径")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        html = f.read()
    parser = BookmarkParser()
    parser.feed(html)
    bookmarks = parser.bookmarks

    if args.mode == "summary":
        res = analyze_summary(bookmarks)
        text = summary_text(res)
    else:
        s = analyze_summary(bookmarks)
        d = analyze_deadlinks(bookmarks, limit=args.limit)
        res = {"summary": s, "deadlinks": d}
        text = summary_text(s) + "\n\n" + deadlink_text(d)

    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"\n[JSON] 已写入: {args.json}")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"[报告] 已写入: {args.report}")


if __name__ == "__main__":
    main()

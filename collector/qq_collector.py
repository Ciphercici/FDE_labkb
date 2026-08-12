#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 群公众号文章自动收藏机器人（OneBot 11 协议，配合 NapCat 使用）

流程: 群消息 → 提取 mp.weixin.qq.com 链接 → 调 wechat_fetcher 抓取入库 → 群内回复结果 → git 自动提交

部署:
    1. 下载运行 NapCat（QQNT 协议库），登录专用机器人账号
    2. NapCat WebUI 开启 WebSocket 服务端（默认 ws://127.0.0.1:3001）
    3. pip install websockets
    4. 修改下方 CONFIG（ws_url / kb_root / fetcher 路径 / 群白名单）
    5. python qq_collector.py

依赖: pip install websockets
"""

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import websockets

# ============ 配置（按需修改） ============
CONFIG = {
    "ws_url": "ws://127.0.0.1:3001",       # NapCat 正向 WebSocket 地址
    "group_whitelist": [],  # 填自己的群号，如 [123456789]        # 只处理这些群，[] = 所有群（建议填自己的群号）
    "kb_root": r"<kb_root>",  # 知识库根目录
    "fetcher": "collector/wechat_fetcher.py",                        # 相对 kb_root（正斜杠，跨平台）
    "git_auto_commit": True,                # 抓取成功后自动 git add + commit
    "git_auto_push": False,                 # 是否自动 push（凭据自行配置，不写明文）
    "cooldown_sec": 300,                    # 同一链接去重冷却（秒）
    "fetch_mode": "local",                  # local=收到链接直接抓取(本机)；queue=入队待处理端统一抓取(服务器网络受限时)
}
# ==========================================

WECHAT_URL_RE = re.compile(r"https?://mp\.weixin\.qq\.com/[^\s，,。；;\"'<>]+")
_seen = {}  # url -> last_time


def extract_wechat_urls(data):
    """从消息事件里提取公众号链接（纯文本 + CQ share 段）"""
    texts = [data.get("raw_message") or ""]
    for seg in data.get("message", []):
        if seg.get("type") == "text":
            texts.append(seg.get("data", {}).get("text", ""))
        elif seg.get("type") == "share":
            texts.append(seg.get("data", {}).get("url", ""))
    urls, seen = [], set()
    for t in texts:
        for u in WECHAT_URL_RE.findall(t):
            if u not in seen:  # raw_message 与 text 段可能重复
                seen.add(u)
                urls.append(u)
    return urls


def dedup(urls):
    """冷却期内重复链接过滤"""
    now = time.time()
    fresh = []
    for u in urls:
        if now - _seen.get(u, 0) > CONFIG["cooldown_sec"]:
            _seen[u] = now
            fresh.append(u)
    return fresh


def run_fetcher(url):
    """调抓取器，返回 (ok, 回复文本)"""
    kb = Path(CONFIG["kb_root"])
    fetcher = kb / CONFIG["fetcher"]
    out_dir = kb / "05_知识" / "文章收藏"
    att_dir = kb / "_attachments"
    cmd = [
        sys.executable, str(fetcher), url,
        "--out", str(out_dir), "--attachments", str(att_dir),
        "--kb-root", str(kb),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, "抓取超时（>120s），稍后重试或人工打开链接"
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        reason = err.splitlines()[-1] if err else "未知错误"
        return False, f"抓取失败：{reason}"

    # 解析成功输出
    lines = out.splitlines()
    saved = next((l for l in lines if l.startswith("已保存")), "")
    stats = next((l for l in lines if l.startswith("图片")), "")
    title = next((l for l in lines if l.startswith("标题")), "")
    try:
        rel = str(Path(saved.replace("已保存: ", "").strip()).resolve().relative_to(kb.resolve()))
    except Exception:
        rel = saved.replace("已保存: ", "").replace("\\", "/")
    img_ok, img_fail = 0, 0
    m = re.search(r"(\d+) 成功 / (\d+) 失败", stats or "")
    if m:
        img_ok, img_fail = int(m.group(1)), int(m.group(2))
    head = f"已收藏 ✅ {rel}"
    tail = []
    if title:
        tail.append(title)
    if img_ok or img_fail:
        tail.append(f"图片 {img_ok}/{img_ok + img_fail} 成功"
                    + (f"，{img_fail} 张失败留占位" if img_fail else ""))
    git_note = ""
    if CONFIG["git_auto_commit"]:
        git_note = auto_commit(kb)
    return True, "\n".join([head, *tail, git_note]).strip()


def auto_commit(kb):
    """抓取成功 → git add + commit（失败静默，不阻塞回复）"""
    try:
        if not (kb / ".git").exists():
            return "（知识库尚未 git init，跳过提交）"
        for d in ("05_知识/文章收藏", "_attachments"):
            subprocess.run(["git", "add", "-A", d], cwd=kb,
                           capture_output=True, text=True, timeout=30)
        r = subprocess.run(
            ["git", "commit", "-m", f"bot: 收藏公众号文章 {time.strftime('%Y-%m-%d %H:%M')}"],
            cwd=kb, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            if CONFIG["git_auto_push"]:
                p = subprocess.run(
                    ["git", "push", "origin", "HEAD"],
                    cwd=kb, capture_output=True, text=True, timeout=60)
                if p.returncode == 0:
                    return "git 已提交并推送 ✓"
                return f"git 已提交，推送失败：{(p.stderr or p.stdout).strip()[-60:]}"
            return "git 已提交 ✓"
        if "nothing to commit" in (r.stdout + r.stderr):
            return ""
        return f"（git commit 提示：{r.stderr.strip()[:60]}）"
    except Exception:
        return ""


def enqueue_links(kb, urls):
    """链接写入队列文件 _queue/links.txt（去重）"""
    q = kb / "_queue" / "links.txt"
    q.parent.mkdir(parents=True, exist_ok=True)
    existing = set(q.read_text(encoding="utf-8").splitlines()) if q.exists() else set()
    added = [u for u in urls if u not in existing]
    if added:
        with q.open("a", encoding="utf-8") as f:
            f.write("\n".join(added) + "\n")
    return added


def git_push_file(kb, relpath):
    """提交并推送单个文件（队列同步用）；返回状态标记"""
    try:
        if not (kb / ".git").exists():
            return "（未 init）"
        subprocess.run(["git", "add", relpath], cwd=kb,
                       capture_output=True, text=True, timeout=30)
        r = subprocess.run(
            ["git", "commit", "-m", f"bot: 队列更新 {time.strftime('%Y-%m-%d %H:%M')}"],
            cwd=kb, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return "（无变更）"
        p = subprocess.run(["git", "push", "origin", "HEAD"], cwd=kb,
                           capture_output=True, text=True, timeout=60)
        return "✓" if p.returncode == 0 else "✗推送失败"
    except Exception:
        return ""


async def handle_event(ws, data):
    """处理一条消息事件：提取链接 → 抓取（local）或入队（queue）→ 回复"""
    if data.get("post_type") != "message" or data.get("message_type") != "group":
        return
    gid = data.get("group_id")
    if CONFIG["group_whitelist"] and gid not in CONFIG["group_whitelist"]:
        return
    urls = dedup(extract_wechat_urls(data))
    if not urls:
        return

    if CONFIG["fetch_mode"] == "queue":
        # 服务器模式（网络受限不抓取）：入队，处理端（本机/云函数）统一抓取
        # 先同步远端队列：服务器只 push 不 pull，若本机已清空队列而服务器本地是
        # 旧文件，append 会把已处理的链接重复推上去 → 入队前先 pull 对齐
        kb = Path(CONFIG["kb_root"])
        subprocess.run(["git", "pull", "--autostash", "origin", "main"],
                       cwd=kb, capture_output=True, text=True, timeout=60)
        added = enqueue_links(kb, urls)
        git_note = ""
        if added:
            git_note = git_push_file(Path(CONFIG["kb_root"]), "_queue/links.txt")
        reply = (f"已收到 {len(added)} 条链接，排队中 📥\n云函数自动抓取入库（约 10 分钟内）"
                 if added else "该链接已在队列中 📥")
        if git_note:
            reply += f"（git {git_note}）"
        try:
            await ws.send(json.dumps({
                "action": "send_group_msg",
                "params": {"group_id": gid, "message": reply},
            }, ensure_ascii=False))
        except Exception:
            pass
        return

    for u in urls:
        _, reply = run_fetcher(u)
        try:
            await ws.send(json.dumps({
                "action": "send_group_msg",
                "params": {"group_id": gid, "message": reply},
            }, ensure_ascii=False))
        except Exception:
            pass


async def run():
    while True:
        try:
            async with websockets.connect(CONFIG["ws_url"]) as ws:
                print(f"[{time.strftime('%H:%M:%S')}] 已连接 NapCat {CONFIG['ws_url']}")
                while True:
                    raw = await ws.recv()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    await handle_event(ws, data)
        except (websockets.ConnectionClosed, OSError) as e:
            print(f"[{time.strftime('%H:%M:%S')}] 连接断开（{e}），5 秒后重连")
            await asyncio.sleep(5)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("QQ 公众号收藏机器人启动...")
    print(f"监听: {CONFIG['ws_url']} | 知识库: {CONFIG['kb_root']}")
    if CONFIG["group_whitelist"]:
        print(f"群白名单: {CONFIG['group_whitelist']}")
    else:
        print("群白名单: 空（处理所有群，建议配置）")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n已退出")

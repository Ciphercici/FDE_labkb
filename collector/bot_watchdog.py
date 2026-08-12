#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 机器人掉线看门狗（服务器运行，crontab 每 5 分钟）

背景: 机房网络下 IM 会话可能被服务端判定异常而失效（"网络连接异常"1006514 /
      "身份已失效，为保证账号安全"），NapCat 不会自动重连。
     本脚本每 5 分钟检查 NapCat 日志错误，发现掉线后自动重启容器
     （尝试 -e ACCOUNT 快速登录恢复）；快速登录也失败（登录态被服务端
     作废）则写入 _queue/bot_status.md 并 push 到 Gitee —— 用户在
     Obsidian 里 pull 即看到"需要人工重新登录"提醒。

部署（服务器）:
    crontab -e  添加:
    */5 * * * * python3 /home/user/lab-kb/collector/bot_watchdog.py

检查状态:
    cat _queue/bot_status.md   （存在=需人工重登；不存在=正常）
"""

import subprocess
import time
from pathlib import Path

KB = Path("/home/user/lab-kb")
STATUS = KB / "_queue" / "bot_status.md"
LOG = Path("/tmp/bot_watchdog.log")

GIT_REMOTE = "https://<git平台>/<org>/<repo>.git"  # 仅供注释参考，实际用仓库已配 remote


def log(msg):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def run(cmd, timeout=60):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def push_status(text):
    """写状态文件并提交推送（失败静默，下轮重试）"""
    try:
        STATUS.write_text(text, encoding="utf-8")
        subprocess.run(f"cd {KB} && git add _queue/bot_status.md", shell=True,
                       capture_output=True, timeout=30)
        r = subprocess.run(
            f"cd {KB} && git commit -q -m 'bot: 机器人掉线需人工重登 {time.strftime('%m-%d %H:%M')}'",
            shell=True, capture_output=True, timeout=30)
        if r.returncode == 0 or "nothing to commit" in (r.stdout + r.stderr):
            subprocess.run(f"cd {KB} && git push -q origin main", shell=True,
                           capture_output=True, timeout=60)
            log("状态文件已推送")
    except Exception as e:
        log(f"push_status 异常: {e}")


def clear_status():
    """恢复正常：删除状态文件并推送（无变更则跳过）"""
    if not STATUS.exists():
        return
    try:
        STATUS.unlink(missing_ok=True)
        subprocess.run(f"cd {KB} && git add _queue/bot_status.md", shell=True,
                       capture_output=True, timeout=30)
        r = subprocess.run(
            f"cd {KB} && git commit -q -m 'bot: 机器人已恢复在线 {time.strftime('%m-%d %H:%M')}'",
            shell=True, capture_output=True, timeout=30)
        if r.returncode == 0 or "nothing to commit" in (r.stdout + r.stderr):
            subprocess.run(f"cd {KB} && git push -q origin main", shell=True,
                           capture_output=True, timeout=60)
        log("已清除状态文件")
    except Exception as e:
        log(f"clear_status 异常: {e}")


def main():
    # 1. 检查 NapCat 最近 8 分钟日志是否有掉线/错误标记
    err = run("sudo docker logs napcat --since 8m 2>&1 | "
              "grep -ciE 'EventChecker Failed|身份已失效|网络连接异常|1006514'")
    if not err or err == "0":
        clear_status()
        return

    # 2. 有错误：自动重启容器，尝试快速登录恢复
    log(f"检测到掉线标记（{err} 条），重启 NapCat")
    run("sudo docker restart napcat", timeout=90)
    time.sleep(30)

    # 3. 重启后 30 秒内日志是否仍报"身份已失效"（登录态被服务端作废 → 必须人工）
    bad = run("sudo docker logs napcat --since 40s 2>&1 | grep -c '身份已失效'")
    if bad and bad != "0":
        msg = (
            "⚠️ **机器人掉线，需要人工重新登录**\n\n"
            f"时间：{time.strftime('%Y-%m-%d %H:%M')}\n"
            "原因：QQ 会话被服务端判定异常失效，自动恢复失败\n\n"
            "操作步骤：\n"
            "1. 本机开隧道：`ssh -L 6099:127.0.0.1:6099 user@<server_ip>`\n"
            "2. 浏览器打开 `http://127.0.0.1:6099/webui?token=<webui_token>`\n"
            "3. 专用账号 <bot_qq> 密码 + 短信验证码登录\n\n"
            "登录后本文件会被自动清除（watchdog 检测到恢复）。"
        )
        push_status(msg)
        log("快速登录失败，已推送人工重登提醒")
    else:
        log("重启后未见'身份已失效'，等待下轮确认恢复")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"watchdog 异常: {e}")

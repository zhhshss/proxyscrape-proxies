#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取 proxyscrape 免费公共代理列表 → node/free_proxies_<ts>.txt

数据源: https://api.proxyscrape.com/v4/free-proxy-list/get（无需账号/验证码）
本机大陆需走代理，GHA 海外 runner 直连。

用法:
    python3 fetch_free.py [数量]     # 默认 1500，最多 2000
环境:
    PS_PROXY=        # 本机 http://127.0.0.1:7890；GHA 留空直连
输出:
    node/free_proxies_<ts>.txt  每行 protocol://ip:port
    stdout 打印 NODE_FILE=<路径>
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import register as R

API = ("https://api.proxyscrape.com/v4/free-proxy-list/get"
       "?request=display_proxies&proxy_format=ipport&format=json")


def fetch(limit):
    import requests
    r = requests.get(API, params={"limit": min(int(limit), 2000)},
                     proxies=R.PROXIES, timeout=60)
    r.raise_for_status()
    return r.json()["proxies"]


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    ps = fetch(limit)
    if not ps:
        print("[!] 列表为空", file=sys.stderr)
        sys.exit(1)

    # 过滤：alive + 有协议 + 排除透明代理（可选）+ 按 uptime 排序取前 limit
    def score(p):
        return float(p.get("uptime") or 0)

    keep = [p for p in ps if p.get("alive") and p.get("protocol") and p.get("ip")]
    keep.sort(key=score, reverse=True)
    keep = keep[:limit]

    # 输出 protocol://ip:port（免费代理无账密）
    lines = []
    for p in keep:
        proto = p["protocol"]
        addr = f"{proto}://{p['ip']}:{p['port']}"
        lines.append((addr, float(p.get("uptime") or 0)))

    if not lines:
        print("[!] 过滤后为空", file=sys.stderr)
        sys.exit(1)

    ts = time.strftime("%Y%m%d_%H%M%S")
    node_file = os.path.join(R._NODE_DIR, f"free_proxies_{ts}.txt")
    with open(node_file, "w", encoding="utf-8") as f:
        for addr, up in lines:
            f.write(addr + "\n")

    print(f"拉取免费代理 {len(lines)} 个（含 http/socks）", flush=True)
    print(f"NODE_FILE={node_file}", flush=True)
    # 附统计
    import collections
    c = collections.Counter(a.split("://")[0] for a, _ in lines)
    print("协议分布:", dict(c), flush=True)


if __name__ == "__main__":
    main()

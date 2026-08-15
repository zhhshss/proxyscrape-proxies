#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传代理批次到 Cloudflare KV 并清理 6 天前的旧批次。

用法:
    python3 kv.py <proxies.txt>
环境（二选一认证）:
    方式 A（API Token）:  CLOUDFLARE_API_TOKEN
    方式 B（Global API Key）:  CLOUDFLARE_API_KEY + CLOUDFLARE_API_EMAIL
    公共:                  CLOUDFLARE_ACCOUNT_ID、KV_NAMESPACE_ID

KV key 结构:
    proxyscrape:latest       固定 key，始终指向最新活跃代理（方便 Worker 读）
    proxyscrape:YYYY-MM-DD   该日期批次
清理逻辑: 上传今天批次后，删除 6 天前的 proxyscrape:<date-6d>。
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

API = "https://api.cloudflare.com/client/v4"
ACCOUNT = os.environ["CLOUDFLARE_ACCOUNT_ID"]
NS = os.environ["KV_NAMESPACE_ID"]

TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
KEY = os.environ.get("CLOUDFLARE_API_KEY", "").strip()
EMAIL = os.environ.get("CLOUDFLARE_API_EMAIL", "").strip()


def _auth_headers():
    if TOKEN:
        return {"Authorization": f"Bearer {TOKEN}"}
    return {"X-Auth-Email": EMAIL, "X-Auth-Key": KEY}


def _req(method, path, body=None, ctype=None):
    url = f"{API}{path}"
    req = urllib.request.Request(url, method=method, data=body)
    h = _auth_headers()
    for k, v in h.items():
        req.add_header(k, v)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def put_value(key, value):
    k = quote(key, safe="")
    path = f"/accounts/{ACCOUNT}/storage/kv/namespaces/{NS}/values/{k}"
    status, body = _req("PUT", path, body=value.encode("utf-8"), ctype="text/plain")
    print(f"PUT  {key} -> {status} {body[:150]}", flush=True)
    return status


def delete_key(key):
    k = quote(key, safe="")
    path = f"/accounts/{ACCOUNT}/storage/kv/namespaces/{NS}/values/{k}"
    status, body = _req("DELETE", path)
    print(f"DEL  {key} -> {status} {body[:150]}", flush=True)
    return status


def main():
    f = sys.argv[1] if len(sys.argv) > 1 else None
    if not f or not os.path.exists(f):
        print(f"用法: {os.path.basename(sys.argv[0])} <proxies.txt>", file=sys.stderr)
        sys.exit(1)

    with open(f, encoding="utf-8") as fh:
        content = fh.read().strip()
    lines = [l for l in content.splitlines() if l.strip()]
    if not lines:
        print("[!] 代理文件为空，跳过上传", file=sys.stderr)
        sys.exit(0)
    print(f"批次代理数: {len(lines)}", flush=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    old = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")

    put_value(f"proxyscrape:{today}", content)
    put_value("proxyscrape:latest", content)
    delete_key(f"proxyscrape:{old}")
    print("完成", flush=True)


if __name__ == "__main__":
    main()

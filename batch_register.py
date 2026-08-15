#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量注册 N 个 ProxyScrape 账号并拉取代理，汇总到 node/。

用法:
    python3 batch_register.py              # 默认 3 个
    python3 batch_register.py 5            # 注册 5 个
环境:
    PS_PROXY=              # GHA/海外环境留空走直连；本地大陆填 http://127.0.0.1:7890
    YYDS_API_KEY=          # 可选，切到 YYDS 临时邮箱（默认 mail.tm）
输出:
    account/accounts_<ts>.jsonl  账号（含 token）
    node/proxies_<ts>.txt        代理 user:pass@ip:port
    stdout 打印 NODE_FILE=<路径> 供后续步骤使用
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import register as R


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    ts = time.strftime("%Y%m%d_%H%M%S")
    acc_file = os.path.join(R._ACCOUNT_DIR, f"accounts_{ts}.jsonl")
    node_file = os.path.join(R._NODE_DIR, f"proxies_{ts}.txt")

    ok = 0
    for i in range(count):
        try:
            rec = R.register_one(i + 1, headless=True, acc_file=acc_file,
                                 node_file=node_file, max_attempts=2)
            if rec:
                ok += 1
        except Exception as e:
            print(f"[x] #{i + 1} 失败: {str(e)[:150]}", flush=True)

    proxies = []
    if os.path.exists(node_file):
        with open(node_file, encoding="utf-8") as fh:
            proxies = [l.strip() for l in fh if l.strip()]
    print(f"完成 {ok}/{count} 个账号，代理共 {len(proxies)} 个", flush=True)
    print(f"NODE_FILE={node_file}", flush=True)
    return node_file, proxies


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""链式 CONNECT 代理：本地 HTTP proxy -> [mihomo] -> ISP 代理 -> 目标

两种模式（由 CHAIN_DIRECT 环境变量决定，默认 false）：
  · 本机大陆（默认）：   curl -> chain_proxy -> mihomo(7890) -> isp_proxy -> target
  · GHA/海外（DIRECT=1）：curl -> chain_proxy -> isp_proxy -> target（runner 海外直连 ISP）

用法:
    python3 chain_proxy.py <listen_port>              # 本机（经 mihomo）
    CHAIN_DIRECT=1 ISP_FILE=isp.txt python3 chain_proxy.py 18791   # GHA
ISP 代理列表文件每行: https://user:pass@ip:port#[任意注释]
"""
import socket
import threading
import struct
import sys
import os
import base64
import ssl

CHAIN_DIRECT = os.environ.get("CHAIN_DIRECT", "").strip().lower() in {"1", "true", "yes"}
ISP_FILE = os.environ.get("ISP_FILE", "/root/.claude/jobs/17f2ec36/tmp/isp_ok.txt")
MIHOMO = ("127.0.0.1", 7890)
LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18791

# 目标 ISP 代理列表（ip, port, user, pass）
ISPS = []
for line in open(ISP_FILE):
    line = line.strip()
    if not line:
        continue
    addr = line.split("#")[0].strip()          # https://user:pass@ip:443
    body = addr.replace("https://", "").replace("http://", "")
    user = pwd = ""
    if "@" in body:
        cred, body = body.rsplit("@", 1)
        if ":" in cred:
            user, pwd = cred.split(":", 1)
    ip, port = body.rsplit(":", 1)
    ISPS.append((ip, int(port), user, pwd))


def connect_via_mihomo(host, port, timeout=25):
    s = socket.create_connection(MIHOMO, timeout=timeout)
    s.settimeout(timeout)
    s.sendall(b"\x05\x01\x00")
    r = s.recv(2)
    if len(r) < 2 or r[0] != 5 or r[1] != 0:
        raise RuntimeError("socks5 auth method rejected: %r" % r)
    if ":" in host:
        s.sendall(b"\x05\x01\x00\x04" + socket.inet_pton(socket.AF_INET6, host) + struct.pack(">H", port))
    else:
        s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(host) + struct.pack(">H", port))
    r = s.recv(4)
    if len(r) < 4 or r[1] != 0:
        raise RuntimeError("socks5 connect failed: %r" % r)
    atype = r[3]
    if atype == 1:
        s.recv(6)
    elif atype == 3:
        l = s.recv(1)[0]
        s.recv(l + 2)
    elif atype == 4:
        s.recv(18)
    return s


def connect_to_isp(isp):
    """连到 ISP 代理（经 mihomo 或直连），返回原始 TCP socket。"""
    ip, port, user, pwd = isp
    if CHAIN_DIRECT:
        s = socket.create_connection((ip, port), timeout=25)
        s.settimeout(30)
        return s
    return connect_via_mihomo(ip, port)


def connect_through_isp(isp, target_host, target_port):
    """连到 ISP 代理（443 需 TLS 包裹），再 CONNECT 到目标。"""
    ip, port, user, pwd = isp
    raw = connect_to_isp(isp)
    # ISP 代理是 https:// 前缀 = 代理本身走 TLS
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        up = ctx.wrap_socket(raw, server_hostname=ip)
    except Exception:
        up = raw
    req = b"CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n" % (
        target_host.encode(), target_port, target_host.encode(), target_port)
    if user:
        auth = base64.b64encode(("%s:%s" % (user, pwd)).encode()).decode()
        req += b"Proxy-Authorization: Basic %s\r\n" % auth.encode()
    req += b"\r\n"
    up.sendall(req)
    data = b""
    while b"\r\n\r\n" not in data:
        d = up.recv(4096)
        if not d:
            break
        data += d
    first = data.split(b"\r\n")[0]
    if b"200" not in first:
        up.close()
        raise RuntimeError("ISP CONNECT failed: %s" % first.decode(errors="replace"))
    return up


def pipe(src, dst):
    try:
        while True:
            d = src.recv(65536)
            if not d:
                break
            dst.sendall(d)
    except Exception:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass


def handle(client, isp):
    try:
        client.settimeout(20)
        data = b""
        while b"\r\n\r\n" not in data:
            d = client.recv(4096)
            if not d:
                break
            data += d
        line = data.split(b"\r\n")[0]
        parts = line.split(b" ")
        if len(parts) < 3 or parts[0] != b"CONNECT":
            client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        target = parts[1].decode()
        host, ps = target.rsplit(":", 1)
        port = int(ps)
        up = connect_through_isp(isp, host, port)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        t1 = threading.Thread(target=pipe, args=(client, up), daemon=True)
        t2 = threading.Thread(target=pipe, args=(up, client), daemon=True)
        t1.start(); t2.start()
        t1.join(); t2.join()
    except Exception as e:
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        except Exception:
            pass
    finally:
        try:
            client.close()
        except Exception:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", LISTEN_PORT))
    srv.listen(64)
    print(f"chain_proxy listening on {LISTEN_PORT}, {len(ISPS)} isps, direct={CHAIN_DIRECT}", flush=True)
    idx = 0
    while True:
        c, _ = srv.accept()
        isp = ISPS[idx % len(ISPS)]
        idx += 1
        threading.Thread(target=handle, args=(c, isp), daemon=True).start()


if __name__ == "__main__":
    main()

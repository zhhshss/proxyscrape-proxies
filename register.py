# -*- coding: utf-8 -*-
"""
ProxyScrape dashboard 注册机 - 完整版

逆向结论（dashboard.proxyscrape.com/v2，Next.js + Cloudflare Turnstile）：
  · 注册端点   POST /v2/v4/account/auth/register
      字段（application/x-www-form-urlencoded）: email / password / cf_turnstile_token
      无 token 时返回 400 "missing-input-response"，证明 Turnstile 是服务端校验的硬门槛。
  · 邮箱验证   POST /v2/v4/account/verify-email
      字段 verificationCode（multipart/form-data，注意不是 code）
  · 重发验证码 POST /v2/v4/account/reset-verification-code（注册后不会自动发，必须先调一次）
  · Turnstile sitekey = 0x4AAAAAAAFWUVCKyusT9T8r

流程：临时邮箱 → 本地浏览器出 Turnstile token → 协议注册 → 触发重发验证码 →
      收信取码 → 验邮箱 → 拉免费 datacenter 代理（trial 自带 100 个）。

依赖：DrissionPage / requests（系统 python3 已装，或用 grok 项目 .venv）
网络：dashboard 直连不通，必须走代理。默认 http://127.0.0.1:7890 (mihomo)。
邮箱：默认 mail.tm（免费零配置）；设 YYDS_API_KEY 后自动切到 YYDS。
用法：
    python3 register.py            # 交互式：问注册数量/并发/是否隐藏窗口
    PS_PROXY=socks5h://127.0.0.1:7890 python3 register.py
"""

import os
import re
import sys
import time
import json
import random
import string
import threading
import html as _html
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 常量 ────────────────────────────────────────────────
PS_BASE = "https://dashboard.proxyscrape.com"
PS_REGISTER = f"{PS_BASE}/v2/v4/account/auth/register"
PS_LOGIN = f"{PS_BASE}/v2/v4/account/auth/login"
PS_ME = f"{PS_BASE}/v2/v4/account/auth/me"
PS_VERIFY_EMAIL = f"{PS_BASE}/v2/v4/account/verify-email"
PS_RESEND = f"{PS_BASE}/v2/v4/account/reset-verification-code"
PS_SIGNUP_PAGE = f"{PS_BASE}/v2/sign-up"
PS_SITEKEY = "0x4AAAAAAAFWUVCKyusT9T8r"

PROXY = os.environ.get("PS_PROXY", "http://127.0.0.1:7890").strip()
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

# 邮箱 API 的代理与 dashboard 分开：mail.tm/maliapi 走直连或 PS_MAIL_PROXY，
# 不受 PS_PROXY（可能指向链式 US ISP）影响 —— 链式代理对任意目标不稳定。
_MAIL_PROXY = os.environ.get("PS_MAIL_PROXY", "").strip()
MAIL_PROXIES = {"http": _MAIL_PROXY, "https": _MAIL_PROXY} if _MAIL_PROXY else None

# turnstilePatch 扩展（隐藏自动化标识）。MV2 扩展在旧/定制 Chrome 有效，
# 新版 Chrome 会静默忽略，靠 --disable-blink-features + DrissionPage 伪装兜底。
_TURNSTILE_CANDIDATES = [
    os.environ.get("TURNSTILE_EXTENSION_PATH", "").strip(),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "turnstilePatch"),
    "/root/grok-reg-20260725/turnstilePatch",
    "/root/grok-register-web/turnstilePatch",
    "/root/ivycraft2api/registrar/turnstilePatch",
]
_TURNSTILE_EXT = next((p for p in _TURNSTILE_CANDIDATES if p and os.path.isdir(p)), "")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

_BASE = os.path.dirname(os.path.abspath(__file__))
_ACCOUNT_DIR = os.path.join(_BASE, "account")
_NODE_DIR = os.path.join(_BASE, "node")
os.makedirs(_ACCOUNT_DIR, exist_ok=True)
os.makedirs(_NODE_DIR, exist_ok=True)

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": UA,
    "Origin": PS_BASE,
    "Referer": PS_SIGNUP_PAGE,
}

_print_lock = threading.Lock()
_file_lock = threading.Lock()
_tls = threading.local()


def log(msg):
    tag = getattr(_tls, "tag", "")
    with _print_lock:
        print(f"[{time.strftime('%H:%M:%S')}]{tag} {msg}", flush=True)


def _retry(fn, tries=3, delay=2.0, what=""):
    last = None
    for i in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < tries:
                log(f"[retry {i}/{tries}] {what or 'op'} 失败: {str(e)[:100]}，{delay:.0f}s 后重试")
                time.sleep(delay)
    raise last


# ── 邮箱 provider 抽象 ──────────────────────────────────
class MailProvider:
    def create(self):
        raise NotImplementedError
    def wait_code(self, creds, timeout=180, interval=5):
        raise NotImplementedError


class MailTM(MailProvider):
    BASE = "https://api.mail.tm"

    def _req(self, method, path, **kw):
        return requests.request(method, f"{self.BASE}{path}",
                                proxies=MAIL_PROXIES, timeout=30, **kw)

    def create(self):
        def _do():
            domains = self._req("GET", "/domains").json()
            domain = domains["hydra:member"][0]["domain"]
            local = "ps" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
            address = f"{local}@{domain}"
            pwd = "".join(random.choices(string.ascii_letters + string.digits, k=16))
            self._req("POST", "/accounts", json={"address": address, "password": pwd}).raise_for_status()
            tok = self._req("POST", "/token",
                            json={"address": address, "password": pwd}).json()["token"]
            return address, {"token": tok}
        d = _retry(_do, tries=3, what="mail.tm 建邮箱")
        log(f"临时邮箱: {d[0]}")
        return d

    def wait_code(self, creds, timeout=180, interval=5):
        hdr = {"Authorization": f"Bearer {creds['token']}"}
        deadline = time.time() + timeout
        while time.time() < deadline:
            lst = self._req("GET", "/messages", headers=hdr).json()
            for m in lst.get("hydra:member", []):
                d = self._req("GET", f"/messages/{m['id']}", headers=hdr).json()
                html = " ".join(d.get("html") or [])
                txt = _html.unescape(re.sub(r"<[^>]+>", " ", html))
                mo = re.search(r"verification code:\s*([A-Za-z0-9]{6,})", txt, re.I)
                if mo:
                    log(f"收到验证码: {mo.group(1)}  (主题: {d.get('subject')})")
                    return mo.group(1)
            time.sleep(interval)
        raise TimeoutError("等验证码超时")


class YYDS(MailProvider):
    BASE = "https://maliapi.215.im/v1"

    def __init__(self):
        self.key = os.environ.get("YYDS_API_KEY", "").strip()
        self.domain = os.environ.get("YYDS_DOMAIN", "").strip()
        if not self.key:
            raise RuntimeError("未配置 YYDS_API_KEY")

    def create(self):
        def _do():
            local = "ps" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
            payload = {"localPart": local}
            if self.domain:
                payload["domain"] = self.domain
            r = requests.post(f"{self.BASE}/accounts",
                              headers={"X-API-Key": self.key, "Content-Type": "application/json"},
                              json=payload, proxies=MAIL_PROXIES, timeout=20)
            r.raise_for_status()
            return r.json()["data"]
        d = _retry(_do, tries=3, what="YYDS 建邮箱")
        log(f"临时邮箱: {d['address']}")
        return d["address"], d["token"]

    def wait_code(self, creds, timeout=180, interval=5):
        address = creds
        hdr = {"X-API-Key": self.key}
        deadline = time.time() + timeout
        while time.time() < deadline:
            lst = requests.get(f"{self.BASE}/messages", headers=hdr,
                               params={"address": address, "limit": 5},
                               proxies=MAIL_PROXIES, timeout=30).json()
            for m in lst.get("data", {}).get("messages", []):
                d = requests.get(f"{self.BASE}/messages/{m['id']}", headers=hdr,
                                 params={"address": address},
                                 proxies=MAIL_PROXIES, timeout=30).json().get("data", {})
                html = " ".join(d.get("html") or [])
                txt = _html.unescape(re.sub(r"<[^>]+>", " ", html))
                mo = re.search(r"verification code:\s*([A-Za-z0-9]{6,})", txt, re.I)
                if mo:
                    log(f"收到验证码: {mo.group(1)}  (主题: {d.get('subject')})")
                    return mo.group(1)
            time.sleep(interval)
        raise TimeoutError("等验证码超时")


def get_mail_provider():
    if os.environ.get("YYDS_API_KEY", "").strip():
        return YYDS()
    return MailTM()


# ── 本地打码：浏览器只出 Turnstile token ────────────────
_FILL_JS = r"""
function setVal(el,val){
  var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
  setter.call(el,val);
  el.dispatchEvent(new Event('input',{bubbles:true}));
  el.dispatchEvent(new Event('change',{bubbles:true}));
}
var email=document.querySelector('input[type=email]');
var pwds=document.querySelectorAll('input[type=password]');
var chk=document.querySelector('input[type=checkbox]');
if(email) setVal(email,'warmup'+Date.now()+'@example.com');
if(pwds[0]) setVal(pwds[0],'Warmup123!9');
if(pwds[1]) setVal(pwds[1],'Warmup123!9');
if(chk&&!chk.checked) chk.click();
return 'filled';
"""

_GET_TOKEN_JS = r"""
try{
  var v=String((document.querySelector('input[name="cf-turnstile-response"]')||{}).value||'').trim();
  if(v) return v;
  if(window.turnstile && typeof turnstile.getResponse==='function')
    return String(turnstile.getResponse()||'').trim();
  return '';
}catch(e){ return ''; }
"""

_SCREEN_INJECT_JS = r"""
window.dtp=1;
function ri(a,b){return Math.floor(Math.random()*(b-a+1))+a;}
Object.defineProperty(MouseEvent.prototype,'screenX',{value:ri(800,1200)});
Object.defineProperty(MouseEvent.prototype,'screenY',{value:ri(400,700)});
"""


def solve_turnstile(headless=True, timeout=90):
    """打开真实 sign-up 页，让 Turnstile 自动过（点 checkbox），读 token。"""
    from DrissionPage import Chromium, ChromiumOptions

    opts = ChromiumOptions()
    # auto_port 在本机多任务/残留场景下连不上，改用固定端口+固定目录（GHA 串行够用）
    _port = int(os.environ.get("PS_CHROME_PORT", "9225"))
    opts.set_local_port(_port)
    opts.set_user_data_path(os.path.join(_BASE, ".chrome-%d" % _port))
    if PROXY:
        try:
            opts.set_proxy(PROXY)
        except Exception as e:
            log(f"[!] 设置浏览器代理失败: {e}")
    for flag in ("--no-first-run", "--no-sandbox", "--disable-dev-shm-usage",
                 "--disable-background-networking", "--mute-audio",
                 "--disable-gpu", "--window-size=1280,900",
                 "--disable-blink-features=AutomationControlled"):
        opts.set_argument(flag)
    if os.path.isdir(_TURNSTILE_EXT):
        try:
            opts.add_extension(_TURNSTILE_EXT)
        except Exception as e:
            log(f"[!] 加载 turnstilePatch 扩展失败: {e}")
    else:
        log(f"[!] 未找到 turnstilePatch 扩展: {_TURNSTILE_EXT}")

    browser = Chromium(opts)
    tab = browser.latest_tab
    try:
        log("浏览器打开 sign-up 页…")
        tab.get(PS_SIGNUP_PAGE)
        time.sleep(5)
        tab.run_js(_FILL_JS)
        log("表单已填，预热 Turnstile…")
        time.sleep(2)
        try:
            tab.run_js("try{if(window.turnstile&&turnstile.reset)turnstile.reset()}catch(e){}")
        except Exception:
            pass

        deadline = time.time() + timeout
        while time.time() < deadline:
            token = str(tab.run_js(_GET_TOKEN_JS) or "").strip()
            if len(token) >= 80:
                log(f"Turnstile 通过，token 长度={len(token)}")
                return token

            ci = tab.ele("@name=cf-turnstile-response", timeout=2)
            if ci:
                try:
                    wrapper = ci.parent()
                    iframe = wrapper.shadow_root.ele("tag:iframe", timeout=2)
                except Exception:
                    iframe = None
                if iframe:
                    try:
                        iframe.run_js(_SCREEN_INJECT_JS)
                    except Exception:
                        pass
                    try:
                        body_sr = iframe.ele("tag:body").shadow_root
                        btn = body_sr.ele("tag:input", timeout=2)
                        if btn:
                            btn.click()
                    except Exception:
                        pass
            time.sleep(1.2)
        raise TimeoutError("Turnstile 求解超时")
    finally:
        try:
            browser.quit()
        except Exception:
            pass


# ── 注册协议 ────────────────────────────────────────────
def register(email, password, turnstile_token):
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.post(PS_REGISTER, data={
        "email": email,
        "password": password,
        "cf_turnstile_token": turnstile_token,
    }, proxies=PROXIES, timeout=30)
    log(f"注册响应 {r.status_code}: {r.text[:300]}")
    if r.status_code != 200:
        raise RuntimeError(f"注册失败 HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"注册未返回 access_token: {data}")
    log(f"注册成功，access_token 前缀: {token[:24]}…")
    return s, token, data.get("userData", {})


def resend_code(session, access_token):
    r = session.post(PS_RESEND,
                     headers={"Authorization": f"Bearer {access_token}"},
                     proxies=PROXIES, timeout=30)
    log(f"resend 触发: {r.status_code}")
    return r.ok


def verify_email(session, access_token, code):
    r = session.post(PS_VERIFY_EMAIL,
                     headers={"Authorization": f"Bearer {access_token}"},
                     files={"verificationCode": (None, code)},
                     proxies=PROXIES, timeout=30)
    log(f"验邮箱响应 {r.status_code}: {r.text[:200]}")
    return r.ok


def whoami(session, access_token):
    r = session.post(PS_ME, headers={"Authorization": f"Bearer {access_token}"},
                     proxies=PROXIES, timeout=30)
    log(f"/me {r.status_code}: {r.text[:200]}")
    return r.ok


# ── 拉免费 datacenter 代理 ──────────────────────────────
def fetch_proxies(access_token, account_id):
    h = {"Authorization": f"Bearer {access_token}", "User-Agent": UA, "Origin": PS_BASE}

    def _overview():
        r = requests.get(f"{PS_BASE}/v2/v4/account/{account_id}/services/overview",
                         headers=h, proxies=PROXIES, timeout=25)
        r.raise_for_status()
        data = r.json().get("data")
        if not data or "services" not in data:
            raise RuntimeError(f"overview 无数据: {str(r.text)[:80]}")
        return data

    def _list():
        r = requests.get(f"{PS_BASE}/v2/v4/account/{account_id}/datacenter_shared/proxy-list",
                         headers=h, params={"protocol": "http", "format": "normal"},
                         proxies=PROXIES, timeout=25)
        r.raise_for_status()
        return r.text

    ov = _retry(_overview, tries=3, delay=3, what="overview")
    ds = ov["services"]["datacenter_shared"]
    user, pwd = ds["proxy_username"], ds["proxy_password"]
    txt = _retry(_list, tries=3, delay=3, what="proxy-list")
    lst = [x.strip() for x in txt.split() if ":" in x]
    if not lst:
        raise RuntimeError("proxy-list 为空")
    return user, pwd, lst


def save_account(rec, path):
    with _file_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def save_proxies(user, pwd, proxies, path):
    with _file_lock:
        with open(path, "a", encoding="utf-8") as f:
            for ip in proxies:
                f.write(f"{user}:{pwd}@{ip}\n")


# ── 单账号注册 ──────────────────────────────────────────
def register_once(mail, headless, node_file):
    password = "Ps" + "".join(random.choices(string.ascii_letters + string.digits, k=10)) + "!9"
    email, creds = mail.create()
    token = solve_turnstile(headless=headless)
    session, access_token, userdata = register(email, password, token)

    verified = False
    try:
        resend_code(session, access_token)
        code = mail.wait_code(creds, timeout=180)
        verified = verify_email(session, access_token, code)
    except Exception as e:
        log(f"[!] 邮箱验证环节: {e}（账号已注册，token 有效）")

    p_user = p_pass = ""
    p_count = 0
    if not verified:
        log("邮箱未验证，trial 未激活，跳过拉代理")
    else:
        try:
            subs = userdata.get("associatedSubaccounts") or []
            aid = subs[0].get("AccountID") if subs else None
            if aid:
                p_user, p_pass, plist = fetch_proxies(access_token, aid)
                save_proxies(p_user, p_pass, plist, node_file)
                p_count = len(plist)
                log(f"拉取代理 {p_count} 个")
        except Exception as e:
            log(f"[!] 拉代理失败: {e}")

    return {
        "email": email, "password": password,
        "access_token": access_token, "userData": userdata,
        "mail_creds": creds,          # 邮箱凭据（mail.tm token 等），用于后续重新收信
        "verified": verified,
        "proxy_username": p_user, "proxy_password": p_pass, "proxy_count": p_count,
        "ts": int(time.time()),
    }


def register_one(idx, headless, acc_file, node_file, max_attempts=2):
    _tls.tag = f" #{idx}"
    mail = get_mail_provider()
    last = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            log(f"—— 第 {attempt}/{max_attempts} 次尝试 ——")
        try:
            rec = register_once(mail, headless, node_file)
        except Exception as e:
            log(f"[x] 本次尝试失败: {str(e)[:120]}")
            rec = None
        if rec:
            last = rec
            save_account(rec, acc_file)
            log(f"[{'✓' if rec.get('verified') else '!'}] 完成  verified={rec['verified']}  proxies={rec.get('proxy_count',0)}  {rec['email']}")
            return rec
    return last


# ── 引导 ────────────────────────────────────────────────
def _ask(prompt, default):
    try:
        v = input(prompt).strip()
    except EOFError:
        v = ""
    return v or default


def main():
    print("=" * 56)
    print("   ProxyScrape dashboard 注册机")
    print("   打码: 本地浏览器出 Turnstile token；注册/收信/验证走协议")
    print(f"   代理: {PROXY or '（直连）'}   邮箱: {'YYDS' if os.environ.get('YYDS_API_KEY') else 'mail.tm'}")
    print("=" * 56)
    try:
        count = int(_ask("① 注册数量 [默认 1，输 0 退出]: ", "1"))
    except ValueError:
        count = 1
    if count <= 0:
        print("已退出。")
        return 0
    try:
        threads = int(_ask("② 并发线程 [默认 1]: ", "1"))
    except ValueError:
        threads = 1
    hl = _ask("③ 隐藏浏览器窗口 [Y/n]: ", "Y").lower()
    headless = not hl.startswith("n")
    threads = max(1, min(threads, count, 4))  # 打码浏览器重，并发别太高

    ts = time.strftime("%Y%m%d_%H%M%S")
    acc_file = os.path.join(_ACCOUNT_DIR, f"accounts_{ts}.jsonl")
    node_file = os.path.join(_NODE_DIR, f"proxies_{ts}.txt")

    t0 = time.time()
    ok = []
    if threads == 1:
        for i in range(count):
            r = register_one(i + 1, headless, acc_file, node_file)
            if r:
                ok.append(r)
    else:
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futs = [ex.submit(register_one, i + 1, headless, acc_file, node_file)
                    for i in range(count)]
            for fu in as_completed(futs):
                try:
                    r = fu.result()
                except Exception as e:
                    r = None
                    log(f"[worker error] {e}")
                if r:
                    ok.append(r)

    dt = time.time() - t0
    total = sum(r.get("proxy_count", 0) for r in ok)
    print("\n" + "=" * 56)
    print(f"  完成 {len(ok)}/{count} · 用时 {dt:.0f}s · 代理共 {total} 个")
    print(f"  账号 → account/{os.path.basename(acc_file)}")
    print(f"  代理 → node/{os.path.basename(node_file)}")
    for r in ok:
        print(f"    {r['email']} | {r['password']} | verified={r['verified']} | proxies={r.get('proxy_count',0)}")
    print("=" * 56 + "\n")
    return 0


# ── 最小 DrissionPage 启动调试（用于排查） ─────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--debug":
        print("=== 最小 DrissionPage 测试 ===")
        from DrissionPage import Chromium, ChromiumOptions
        co = ChromiumOptions()
        co.auto_port()
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-gpu")
        try:
            co.set_proxy("http://127.0.0.1:7890")
            print("proxy set ok")
        except Exception as e:
            print("proxy fail", e)
        b = Chromium(co)
        t = b.latest_tab
        t.get("https://dashboard.proxyscrape.com/v2/sign-up")
        print("页面加载成功，title:", t.title[:60])
        b.quit()
        print("BROWSER OK")
        sys.exit(0)
    main()

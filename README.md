# proxyscrape-proxies

ProxyScrape 代理池自动化 + GitHub Actions 定时任务。

## 功能
- **免费公共代理**（主流程，稳定）：拉 api.proxyscrape.com 免费列表 → 上传 Cloudflare KV → 删 6 天前批次
- **账号注册机**：每 6 天注册 3 个 ProxyScrape 账号（US ISP 代理绕 region 封锁 + Turnstile 打码 + 邮箱验证）

## 背景结论（2026-08）
- 注册机在 **US ISP 出口**下完整可用：turnstile → 注册 → 收码 → 验邮箱 全通（`verified=True`）。
- 大陆出口注册被拒："Account registration is not available in your region"。
- **Premium datacenter 免费 trial 当前不存在**（逆向确认）：
  - checkout 页 `type=new_datacenter_shared` 只有 $25 付费，"Claim Free Trial" 按钮只出现在 SERP API；
  - `claim-trial` / `trial-config` 端点只存在于 SERP API；
  - 注册后 `associatedSubaccounts` 为空，无 premium 子账号。
- 因此注册的账号**本身没有代理可拉**；KV 里的代理来自**免费公共列表**（~1400 个含 http/socks）。

## 本地运行
```bash
pip install DrissionPage requests

# 1) 免费列表（代理来源）
PS_PROXY=http://127.0.0.1:7890 python3 fetch_free.py 1500

# 2) 注册账号（大陆出口被 region 封，需 US ISP 链式代理）
#    先起链式代理（读 isp_ok.txt，本机经 mihomo 7890）：
python3 chain_proxy.py 18791
#    再注册（PS_PROXY 指向链式代理；mail.tm 直连不受影响）：
PS_PROXY=http://127.0.0.1:18791 xvfb-run -a -s '-screen 0 1440x900x24' python3 batch_register.py 3

# 3) 上传 KV + 删 6 天前批次
CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID=... KV_NAMESPACE_ID=... python3 kv.py node/proxies_*.txt
```

## GitHub Actions
`.github/workflows/proxyscrape.yml`：
- cron `23 3 6,12,18,24,30 * *`（每月 6/12/18/24/30 号 03:23 UTC，即每 6 天）
- `fetch-and-upload`：拉免费列表 → 上传 KV → 删 6 天前批次
- `register-accounts`：runner 上用 US ISP 代理（direct 模式 chain_proxy）注册 3 个号 → 归档账号

### 需要的仓库 secrets
| Secret | 说明 |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Cloudflare API token（KV 写权限） |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID |
| `KV_NAMESPACE_ID` | KV namespace ID |
| `ISP_PROXIES` | US ISP 代理列表（多行，每行 `https://user:pass@ip:443#[注释]`） |

### KV key 结构
- `proxyscrape:latest` — 固定 key，最新活跃代理（Worker 直接读这个）
- `proxyscrape:YYYY-MM-DD` — 当日批次
- 每轮跑完删除 `proxyscrape:<6天前>` 批次

### Worker 读取示例
```js
export default {
  async fetch(req, env) {
    const v = await env.PROXY_KV.get("proxyscrape:latest");
    return new Response(v, { headers: { "content-type": "text/plain" } });
  },
};
```

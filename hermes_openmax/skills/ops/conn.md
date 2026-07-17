# Conn 外部连接能力说明(暂未提供工具)

**用途**:说明工作区的"外部连接"(Connection)能力 —— owner 授权后,agent 可凭凭据访问外部应用(如 GitHub 等第三方 API)。**当前 hermes-openmax 尚未提供 conn 工具**;本文仅保留能力语义,便于识别场景。

**何时加载本文档**:用户/owner 提到"用我授权的 GitHub/外部应用连接做某事"、需要外部 API 凭据、或排查"连接已授权但 agent 用不了"时。

**本文档不覆盖**:工作区内部的任务/KB/消息/成员/文件操作(见 `ops/` 其他文档)。

**前置条件**:无 —— 当前没有可调用的工具。**遇到需要连接能力的场景,直接告知 owner:conn 工具尚未提供,需要时请安排接入。**

## 能力语义备忘(源平台的 conn 操作面)

| 能力 | 说明 |
| --- | --- |
| 列连接 | 列出本 agent 可用的连接(状态、应用、owner、scopes) |
| 获取凭据 | 为某连接获取凭据,返回 `credential_mode`:**direct** 给真实 `access_token`(+ token_type / expires_at / toolkits),**proxy** 给 `proxy_ref` + `proxy_endpoint` |
| 代理请求 | proxy 模式下把 HTTP 请求(method / url / headers / body)经服务端代理发出,真实凭据由服务端注入、agent 永远看不到;返回 `{status_code, headers, body}` |
| 查连接状态 | 单连接详情:状态、owner、应用、scopes、过期时间 |

### 凭据两种模式

| 模式 | Agent 拿到什么 | 适用 |
|------|-----------|----------|
| **direct** | 真实 access_token | agent 直接带 token 调外部 API |
| **proxy** | proxy_ref 令牌 | agent 调服务端代理;真实凭据不出服务端 |

### 生命周期事件(平台侧)

连接的授权/吊销/断开/凭据更新等事件(`connection.authorized` / `connection.revoked` / `connection.disconnected` / `connection.credential_updated` / `connection.reauth_needed`)由平台在连接层处理;`reauth_needed` 意味着需要 owner 重新授权。凭据缓存等本地机制在我们这里同样由平台管理。

## 当前结论

- **全部 conn 操作当前均无对应工具**(list / acquire / proxy / status / 缓存管理)
- 需要访问外部应用凭据或经连接代理调用第三方 API 时,**如实告知用户该能力暂未接入,并转告 owner 安排**
- 不要尝试用其他工具伪造或绕行(如让用户把 token 粘进聊天)

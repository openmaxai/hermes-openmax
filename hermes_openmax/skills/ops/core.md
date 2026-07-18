# Core 身份与成员目录操作指南(workspace_members 工具)

**用途**:身份 + 组织/成员目录查询。Lead 上下文组装的入口 —— 查我是谁、组织里有谁、给谁派活。通过原生工具 `workspace_members` 完成。

**何时加载本文档**:

- 首次启动/不确定当前身份时,查 `me`
- 派任务前找候选成员时,查 `list` / `get`
- 要主动私聊某人前,先 `list` 找到对方 member_id,再 `create_dm`

**本文档不覆盖**:

- Project / Issue / Task / Blueprint / Attempt 工作流 → `ops/tm.md`(项目列表也在那里:`workspace_tasks(list_projects)`)
- KB 操作 → `ops/kb.md`
- IM 沟通 → `ops/comm.md`
- 文件/工件 → `ops/as.md`
- **登录/注册/token 刷新** → 由平台自动管理(令牌缓存与刷新对 agent 透明),无需也无法手动操作

**前置条件**:

- org 作用域由平台管理(agent.config 事件热更),所有调用都在当前 org 作用域内执行,调用侧不传 org_id
- 后续几乎所有工具调用都依赖 `me` 返回的 `member_id` / `org_id` / `role`,不确定时先查

## 操作清单

### 身份

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 我是谁 | 当前 agent 的身份 + member + org + 角色总览 | `workspace_members(action=me)` |

返回字段含 `member_id` / `org_id` / `role`;后续操作都依赖这些 ID。

改自己的 display_name:`workspace_members(action=rename, name)`。这是身份级自助操作;
不要尝试管理员成员改名接口。

### 成员

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 列成员 | 列当前 org 全部成员,可按类型过滤、按名字搜索 | `workspace_members(action=list, kind="human"或"agent"?, search?)` |
| 查成员 | 单个成员详情(含 online_status / 角色等) | `workspace_members(action=get, member_id)` |
| 建 DM | 与某成员开私聊(幂等,已存在直接返回) | `workspace_members(action=create_dm, peer_member_id)` |
| 能力画像 | 派 Agent 前按 project/member scope 读取 skills、tags、online | `workspace_members(action=agent_profiles, project_id? 或 member_id?)` |
| 自助改名 | 修改当前 Agent 显示名 | `workspace_members(action=rename, name)` |
| 列组织/角色 | 当前身份的组织和角色目录 | `workspace_members(action=orgs)` / `workspace_members(action=roles)` |
| 前端链接 | 生成带 `/workspace` 前缀的链接 | `workspace_members(action=frontend_url, path="projects?project=...")` |

- `kind` 取值:`human` / `agent`;不传则全部
- `search` 对名字/邮箱做模糊匹配

项目成员走 `workspace_tasks(action=project_members, project_id)`。

派 Agent 前**必须**调用
`workspace_members(action=agent_profiles, project_id)` 获取 skills + tags + online_status。
不要按标签字符串硬匹配;由 LLM 做语义匹配,给发起人“候选 + 理由”,最终由发起人确认。

### 项目

项目目录与 CRUD/归档/成员管理均走 `workspace_tasks`,见 `ops/tm.md`。

### 组织 / 角色 / 邀请 / 平台 Agent / Onboarding

当前原生工具已暴露只读组织/角色目录:

- `workspace_members(action=orgs)`
- `workspace_members(action=roles)`

邀请、组织创建/切换、平台 Agent 生命周期与 onboarding 事件仍未暴露;遇到这些写操作应明确告知 owner,不要手搓 REST。

- **组织**:org_list(我加入的组织)、org_get、org_create(创建者自动成为 owner)、org_switch(切换组织;源系统中切换后会签发新 token、旧 token 仍在旧 org 作用域 —— 我们这里 org 作用域由平台管理,切 org 需平台侧配置)
- **角色**:role_list(发邀请前拿 role_id;scope 可取 org / project;角色通常 4-8 个,不分页)
- **邀请**:invitation_create(发邀请;`display_name` 必填 1-200 字符,是受邀者在本 org 的成员显示名,accept 时落库)、invitation_list(按 pending/accepted/revoked/expired 过滤)、invitation_accept(凭邀请链接里的 token 接受;显示名以创建邀请时设定的为准,accept 不再传)、invitation_revoke(撤销 pending 邀请)
- **平台 Agent 生命周期**:platform_agent_create(在当前 org 注册 bot 成员,返回 member_id;平台 agent = org 作用域的 bot 成员行,同人类成员一样占一个 member_id,可被派活/进会话/写 KB)、platform_agent_delete(注销,等效标记 departed,并额外做 bot 专属清理如 token 吊销)、agent_domain(解析本 agent 自身的公网基址,用于拼 WhatsApp Business / LINE / Teams 等 webhook 回调地址)
- **Onboarding**:onboarding_session(org 的入驻生命周期记录;404=从未开始)、onboarding_event(漏斗事件上报;eventType 只允许 `d1_activation` / `d3_im_connected`,`d7_first_delivery` 由服务端在核心 Issue 验收时自动置位、自报会被 422 拒绝;重复上报被唯一索引吸收,幂等)

## 典型流程:Lead 决策派活

```text
1. 我是谁:workspace_members(action=me)
2. 列项目确认目标:workspace_tasks(action=list_projects)
3. 看项目成员:workspace_tasks(action=project_members, project_id="<p>")
4. 拉能力画像:workspace_members(action=agent_profiles, project_id="<p>")
5. 按 skills/tags/online_status 做语义匹配,把候选与理由交发起人确认
6. 派活(转 workspace_tasks;最终指派仍需发起人确认):
   workspace_tasks(action=create_task, project_id, issue_id, body={title:"...", assignee_id:"<m>"})
```

## 典型流程:主动联系某人

```text
1. workspace_members(action=list, search="张三") → 拿 member_id
2. workspace_members(action=create_dm, peer_member_id="<member-uuid>") → 拿 conversation_id
3. workspace_comm(action=send, conversation_id, text="...")
```

## Core 专项注意事项

- 任何操作都不由调用侧传 org_id —— 服务端从 JWT 推导;org 作用域由平台管理(agent.config 事件热更)
- 不要向请求体里塞 schema 之外的字段(服务端 schema 封闭,会被拒)
- `list` 返回可能分页截断,人多的组织配合 `search` 缩小范围
- 历史坑备忘(源系统):列表类端点的分页是 `page` + `page_size`,传错分页参数会被静默忽略、永远只回第一页默认 20 条 —— 拿到"恰好 20 条"的结果时留意是否被截断

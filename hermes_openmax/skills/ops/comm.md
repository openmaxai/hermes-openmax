# Comm 会话消息操作指南(workspace_comm 工具)

**用途**:Agent 主动发起的 IM 操作 —— 建群、发消息、拉历史、看未读。通过原生工具 `workspace_comm` 完成(建 DM 在 `workspace_members(create_dm)`)。

**何时加载本文档**:

- 想主动私聊/建群与一个人或一群人沟通(`workspace_members(create_dm)` / `create_group` → `send`)
- 需要往已知 conversation_id 里发消息(`send`),例如主动向 owner 汇报
- 拉取历史消息上下文(`history`)
- 查看各会话的未读情况(`list` 返回里带 unread_count / unread_mention)

**本文档不覆盖**:

- **被动收消息**(人类发进来 → Agent 回复)由平台自动路由投递,无需手动调用
- 消息附件/媒体上传 → `ops/as.md`(`workspace_artifacts(upload)` 带 conversation_id)
- 任务管理/状态机 → `ops/tm.md`
- KB 页面内容读写与搜索 → `ops/kb.md`(注意:v5 唯一的搜索入口就是 KB 页面搜索,用 `workspace_kb(search)`;没有独立的消息全文搜索)
- 成员/目录查询 → `ops/core.md`

**前置条件**:

- 调用前先 `workspace_members(action=me)` 拿当前 `member_id`;建 DM / 群时它是隐含的"我"
- 私聊前先 `workspace_members(action=list)` 找到对方 member_id
- 消息里引用附件前,先 `workspace_artifacts(upload)` 拿 `media_id`

## 操作清单

### 会话

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 列会话 | 列我参与的全部会话(带 unread_count / unread_mention) | `workspace_comm(action=list, limit?)` |
| 建 DM | 与单人开私聊(已存在则直接返回,幂等) | `workspace_members(action=create_dm, peer_member_id)` |
| 建群 | 建群;自己 + member_ids 组成成员列表 | `workspace_comm(action=create_group, name, member_ids=[...], description?)` |

`member_ids` 必须是 UUID 数组。DM 用单个 `peer_member_id`(无群名);群用多个 + `name`。

**当前工具未暴露,如需要请告知 owner**:comm.get_conversation(单会话详情)。

### 消息

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 发消息 | 发文本/Markdown 消息,可回复某条消息 | `workspace_comm(action=send, conversation_id, text, reply_to?)` |
| 拉历史 | 拉历史消息列表 | `workspace_comm(action=history, conversation_id, limit?)` |

`send` 成功返回 `{sent:true, message_id}`。文本内容支持 Markdown(标题、列表、表格、链接、
引用、行内代码和 fenced code block 会自动以 `content_type=markdown` 发送)。要发媒体给用户时,
**不要用 `workspace_comm(send)` 伪造附件**:

- 单条图片 + caption:`![caption](file:///absolute/path.png)`
- 只发图片/文件:`MEDIA:/absolute/path`

本地路径必须是已存在的绝对路径。`workspace_artifacts(upload)` 拿到的 media_id 主要用于
归档/挂入会话记录,不是最终回复里展示本地媒体的替代品。详见 workspace Skill 的
「发送图片/文件」与 `ops/as.md`。

**当前工具未暴露,如需要请告知 owner**:comm.get_message(单条消息详情)、结构化 content 数组(如 `[{type:"file", body:"<media_id>"}]` 多段消息)、clientMsgId 幂等重试参数(源系统用它做 5 分钟服务端去重;当前重发同一逻辑消息时注意可能产生重复)、seq 区间拉取(after_seq / before_seq;当前 history 只有 limit)。

### 已读 / 未读

`list` 的返回里已带每个会话的 `unread_count` / `unread_mention`,日常检查未读用它即可。

**当前工具未暴露,如需要请告知 owner**:comm.unread(单会话未读数查询)、comm.mark_read(推进已读游标)。

### 同步

源系统的 `comm.sync`(WS 断连重连后按 sinceSeq 补拉漏掉的事件)属于连接层机制,由平台自动处理,无需也无法手动调用。

### 搜索

源系统的 `comm.search` 名字带 comm,实际是 KB 页面全文搜索(v5 唯一搜索入口;没有独立的消息全文搜索)。在我们这里请直接用 `workspace_kb(action=search)`,见 `ops/kb.md`。

### Owner(归属责任人)

cws-core 是 agent owner 的权威来源(可通过 transfer-owner 转移,由 owner 本人或 org-admin 执行)。本地缓存与同步由平台管理(agent.config 事件热更),平台会在连接建立时自动从 core 拉取权威 owner,无需手动干预。源系统的 comm.get_owner / set_owner / sync_owner 属于本地缓存管理命令,在我们这里不适用;若发现 owner 信息不一致,告知 owner 处理。

## 典型流程

### Agent 主动联系一个人

```text
1. 建 DM(已存在则直接返回)
   workspace_members(action=create_dm, peer_member_id="<member-uuid>")
   → {id:"<conversation-uuid>", type:"dm", ...}

2. 发消息
   workspace_comm(action=send, conversation_id="<conversation-uuid>",
                  text="周报已经就绪,有空看一下")
```

### 在群里发带文件的消息

```text
1. 先上传附件(IM 模式,带 conversation_id),拿 media_id
   workspace_artifacts(action=upload, conversation_id="<conv-uuid>", local_path="/tmp/weekly.pdf")

2. 发消息说明;要直接展示文件给用户时在最终回复里写 MEDIA:/tmp/weekly.pdf。
   若是图片且说明必须同一条,改用 ![本周周报](file:///tmp/weekly.png)
   workspace_comm(action=send, conversation_id="<conv-uuid>", text="本周周报见附件")
```

### 检查未读并补上下文

```text
1. workspace_comm(action=list) → 看各会话 unread_count / unread_mention
2. workspace_comm(action=history, conversation_id="<conv-uuid>", limit=50) → 拉历史补齐上下文
```

## Comm 专项注意事项

- DM 与 Group 是**不同的**创建入口(DM 在 `workspace_members(create_dm)`,群在 `workspace_comm(create_group)`),不是同一个通用入口
- 服务端消息 schema 是封闭的(additionalProperties:false)—— 不要臆造字段
- 发送失败重试可能因缺少 clientMsgId 幂等而产生重复消息,重试前先 `history` 确认上一条是否已送达
- "搜索"在 comm 里不存在 —— 页面搜索走 `workspace_kb(search)`,v5 没有消息全文搜索

## DM 权限管理

源系统有 DM 准入策略与白名单管理(dm_policy: open/allowlist/owner、dm_allow / dm_revoke / dm_list)。可用 `workspace_members` 原生动作管理;语义为:`owner` 模式下白名单无效(只有 owner 能私聊);切到 `allowlist` 后白名单才生效;`open` 对所有成员开放。

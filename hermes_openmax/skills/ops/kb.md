# KB 知识库操作指南(workspace_kb 工具)

**用途**:知识库操作 —— 目录树(folder / page / file 节点)、页面内容 + 修订 + 回收站三态模型、跨页面搜索、文件下载。通过原生工具 `workspace_kb` 完成;文件上传到 KB 用 `workspace_artifacts(kb_upload)`。

**何时加载本文档**:

- Lead 上下文组装,搜 KB 找参考材料(`search` + `get_page_content`)
- 沉淀经验/写决策文档/记笔记进 KB(`create_page` / `put_page_content`)
- 整理 KB 目录(`create_folder` / `move_node` / `rename_node`)
- 编辑页面内容 / 回滚到旧修订(`put_page_content` / `revision_restore`)
- 软删页面 / 恢复(`trash` → `restore` 的三态链)
- 把文件登记进 KB 树(`workspace_artifacts(kb_upload)`)
- 通过预签名链接下载 KB 里的文件节点(`download_node`)

**本文档不覆盖**:

- IM 消息附件(走会话):`workspace_artifacts(upload)` 带 conversation_id → `ops/as.md`
- Task / Issue / Blueprint 工作流 → `ops/tm.md`
- 主动发消息 / 建群 → `ops/comm.md`
- 成员/项目目录 → `ops/core.md`

**前置条件**:

- 任何带 `kb_id` 的操作,先 `list_kbs` 确认 KB 存在(每个 org 有 1 个默认 KB,`is_default=true`)
- `create_page` 需要 `parent_id`(文件夹节点 id);从 `tree(kb_id)` 里取
- 需要乐观并发写(base revision)时,先 `get_page` 拿当前 `revision_id`
- org 作用域由平台管理(agent.config 事件热更),无需也无法在调用侧指定

## 数据模型

```
Org(组织,作用域单位)
  └─ KB 配置(每 org 1 个,storage_quota / 搜索开关等)
       │
       ├─ Tree Node(目录树节点)
       │    ├─ kind="folder"  → 文件夹,只有子节点
       │    └─ kind="page"    → 页面壳,关联一个 Page
       │
       └─ Page(内容本体,与树节点 1:1)
            └─ Revision(版本,从 1 自增)
```

## 操作清单

### KB 集合

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 列 KB | 列当前 org 的 KB 实例(目前通常每 org 1 个) | `workspace_kb(action=list_kbs, limit?)` |

**当前工具未暴露,如需要请告知 owner**:kb.init(初始化默认 KB,幂等)、kb.create(建新 KB 实例,visibility=open/closed/private)、kb.get(单 KB 详情)、kb.update(改 KB 元数据)、kb.delete(物理删除 KB,慎用)、kb.archive / kb.unarchive(归档/恢复)。

### 目录树

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 看树 | 获取 KB 的目录树(含根节点与层级) | `workspace_kb(action=tree, kb_id)` |
| 建文件夹 | 创建文件夹节点(KB 唯一的显式节点创建入口) | `workspace_kb(action=create_folder, kb_id, name, parent_id?)` |
| 移动节点 | 把节点移到另一个父节点下(同 KB 内) | `workspace_kb(action=move_node, kb_id, node_id, parent_id?)` |
| 重命名 | 改节点显示名 | `workspace_kb(action=rename_node, kb_id, node_id, name)` |
| 删节点 | 删除节点(文件夹须先清空;page 走各自的 trash → 删除链) | `workspace_kb(action=delete_node, kb_id, node_id)` |
| 下载文件节点 | 获取文件节点的预签名下载 URL | `workspace_kb(action=download_node, kb_id, node_id)` |

**当前工具未暴露,如需要请告知 owner**:kb.node_get(单节点详情)、kb.node_breadcrumb(祖先路径)、kb.node_children(直接子节点;可用 `tree` 整树替代)、kb.file_create(用已有 artifact 登记文件节点)、kb.file_preview(内联预览 URL)、kb.file_batch_download(批量预签名下载)。

节点 ID 形如 `tn-{uuid}`。Page 通过 `create_page` 间接创建对应树节点,不走 create_folder。

**⚠️ page_id ≠ node_id —— 拼 KB 页面链接时 `?node=` 参数必须用 node_id,不是 page_id。** 见下文"前端链接拼装"专节。

### 页面

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 查页面元数据 | 获取页面元数据(含当前 revision_id) | `workspace_kb(action=get_page, page_id)` |
| 建页面 | 创建页面,同时返回页面元数据和 `node_id` | `workspace_kb(action=create_page, kb_id, body={title, parent_id?, format?, body, message?})` |
| 读内容 | 获取页面当前正文 | `workspace_kb(action=get_page_content, page_id)` |
| 写内容 | 只编辑正文(适合大段编辑);支持乐观并发 | `workspace_kb(action=put_page_content, page_id, body={body, message?, base_revision_id?})` |
| 列修订 | 列页面全部修订 | `workspace_kb(action=revisions, page_id, limit?)` |
| 修订 diff | 两个修订之间的 diff(unified 格式) | `workspace_kb(action=revision_diff, page_id, from_rev, to_rev)` |
| 回滚修订 | 把页面内容回滚到旧修订(状态不变,**不是**回收站恢复) | `workspace_kb(action=revision_restore, page_id, revision_id)` |
| 软删 | 软删除(状态 → `trashed`,进回收站) | `workspace_kb(action=trash, page_id)` |
| 回收站恢复 | 从回收站恢复(状态 → `active`,**不是**修订回滚) | `workspace_kb(action=restore, page_id)` |
| 列回收站 | 列当前 org 在回收站的页面 | `workspace_kb(action=trashed, limit?)` |

页面 ID 形如 `pg-{uuid}`。Revision 是每页面从 1 自增的整数。

**当前工具未暴露,如需要请告知 owner**:kb.pages(按父节点列页面;可用 `tree` 替代)、kb.page_update(任意属性 PATCH,含改标题/移动父级 —— 标题/位置可用 `rename_node` / `move_node` 替代,正文用 `put_page_content`)、kb.page_delete(**永久删除**;当前只能软删进回收站)、kb.page_revision(取指定修订的快照内容)、kb.page_freeze(页面只读)、kb.page_references(引用位置列表)。

**写入的乐观并发**:先 `get_page` 拿 `revision_id`,写入时通过 body 传 `base_revision_id`;服务端检测不一致会返回 409 + 当前 revision_id,客户端重读、合并、再写。

**两个"restore"不是一回事,Agent 经常混**:

- `revision_restore`:**回滚到旧修订**;页面状态不变。用于"撤销最近 N 次编辑"。
- `restore`:**从回收站恢复**,状态从 `trashed` 变回 `active`。与修订无关。
- 遇到"恢复"需求,先分清是回收站恢复还是修订回滚,**不要凭名字猜**。

**三态保护链:trash →(永久删除)**:

- 永久删除(物理删除)要求页面已处于 `trashed` 态:必须先 `trash` 扔进回收站。当前工具未暴露永久删除;需要彻底清除时告知 owner。
- 对 `active` 页面直接永久删除,服务端返回 404(语义保护,不要绕过)。
- 完整链:`create_page → ... → trash →(永久删除)`。中途反悔用 `restore` 拉回;**再删时必须重新 trash 一次**。

### 搜索 ⭐

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 全文搜索 | 跨页面全文搜索;ReBAC 过滤(只返回调用者有 viewer+ 权限的页面) | `workspace_kb(action=search, query, kb_id?, limit?)` |

底层:**Meilisearch(模糊 + 容错 + 中文分词)+ NATS 事件驱动索引**。返回结构:

```json
{
  "results": [
    {
      "page": { "id": "pg-...", "title": "...", "path": "...", "format": "markdown" },
      "highlights": [
        { "field": "title", "snippet": "第 21 周<mark>周会纪要</mark>" },
        { "field": "body",  "snippet": "..." }
      ],
      "score": 0.98
    }
  ],
  "pagination": { "next_page_token": null, "total_count": 1 }
}
```

注意:刚写完页面立刻搜索时,异步索引可能还没建好(源系统有 `sync=true` 等索引参数,当前工具未暴露;搜不到刚写的内容时稍等重试)。限流:1000 次/分钟/工作区。按 folder/author/format 过滤的参数当前也未暴露,如需要请告知 owner。

### 文件附件(KB 上传)

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| KB 上传 | 上传本地文件并在 KB 树登记文件节点 | `workspace_artifacts(action=kb_upload, parent_id, local_path)` → 详见 `ops/as.md` |

上传成功后 KB 树中会出现文件节点(响应带 node_id / tree_node);之后可用 `download_node` 操作它。

**不要用 KB 上传发会话附件**:会话/DM 里的图片或文件必须走 **IM 上传**(`workspace_artifacts(upload)` 带 conversation_id,见 `ops/as.md` 开头"该走哪条上传路径"),否则文件挂在 KB 上,对方在聊天窗口里看不到。

返回的 `artifact_id` 也可以写进页面正文(如 markdown 里写 `![](artifact://<id>)`),让页面直接引用该工件。

## 典型流程

### Agent 写一页周会纪要并回读验证

```text
1. 建页面
   workspace_kb(action=create_page, kb_id="<kb-uuid>", body={
     title:"2026-05-21 周会纪要", parent_id:"<文件夹节点 id>",
     format:"markdown", body:"# 2026-05-21 周会纪要\n\n## 议程\n\n...",
     message:"feat: Agent 自动生成周会纪要"})
   → {id:"pg-...", node_id:"tn-...", current_revision_id:1, ...}

2. 搜索验证(索引异步,搜不到时稍等重试)
   workspace_kb(action=search, query="周会纪要", limit=5)
```

### Lead 上下文组装 —— 找项目的设计决策文档

```text
1. 搜索:workspace_kb(action=search, query="架构决策", kb_id="<kb-uuid>", limit=20)
2. 拿到 page_id 后读正文:workspace_kb(action=get_page_content, page_id="pg-arch-decisions-001")
3. 内容存疑时看变更史:
   workspace_kb(action=revisions, page_id="pg-arch-decisions-001")
   workspace_kb(action=revision_diff, page_id="pg-arch-decisions-001", from_rev=3, to_rev=5)
```

### 把 Agent 产出挂到 KB 节点下

```text
1. 上传产出文件(KB 模式):
   workspace_artifacts(action=kb_upload, parent_id="<交付物文件夹节点>", local_path="/tmp/q2-report.pdf")
   → {artifact_id:"art_...", node_id:"tn-...", ...}

2. 写一页交付物索引引用该工件:
   workspace_kb(action=create_page, kb_id="<kb-uuid>", body={
     parent_id:"<交付物文件夹>", title:"Q2 交付物索引",
     body:"# Q2 交付物\n\n- [报告](artifact://art_...)"})
```

## 前端链接拼装(Agent 易错,必读)

KB 页面有**两种 ID**,混用会导致链接打开空白或 404:

| ID 类型 | 来源 | 用途 | 示例 |
|---|---|---|---|
| **page_id** | `create_page` / `get_page` 返回的 `id` | 页面内容操作(读写、修订、回收站) | `019ed02a-62cc-...` |
| **node_id** | `create_page` 返回的 `node_id`,或 `tree` 里的节点 `id` | 目录树操作 + **前端 URL** | `019ed02a-62d5-...` |

**核心规则:前端 URL 的 `node=` 参数只认 node_id,填 page_id 会指向错误位置。**

### 创建响应契约

`create_page` 同时创建页面及其对应树节点,响应顶层是页面对象并附带 `node_id`:

```json
{"id": "019ed02a-62cc-...", "node_id": "019ed02a-62d5-...", "kb_id": "...", "title": "...", "path": "...", ...}
```

`get_page` 只返回页面元数据。若页面不是刚由 `create_page` 返回的,从 `tree` 里定位其节点 ID。

### 正确的链接生成流程

建页后要立即分享链接时,直接使用返回的 `node_id` 拼:

```
{前端基址}/cws/knowledge?kb={kb_id}&node={node_id}
                                          ↑ 必须是树节点 node_id,不是 page_id
```

**反例(错误)**:`...&node=pg-abc123` —— 用了页面 id,链接能打开但空白或指向错误位置。

(源系统有 `core.frontend_url` 一步拼装命令;当前工具未暴露,前端基址如不确定请告知 owner。)

### 手里只有 page_id、不知道 node_id

如果只有 page_id(如从 `search` 结果拿到),用 `get_page` 读 `path` 字段定位目录,再在 `tree` 里对应文件夹下按 page_id 匹配出节点。

## KB 专项注意事项

- org 作用域为必需,由平台管理(agent.config 事件热更),调用侧无需处理
- `list_kbs`:一个 org 通常只有 1 个 KB,但返回数组以便未来扩展
- 页面写入限流:60 次/分钟/用户(超出 429 `rate_limited`)
- `search` 结果经 ReBAC 过滤:只返回调用者有 `viewer+` 权限的页面
- `format` 取值:`markdown` / `code` / `pdf` / `image` / `archive` / `other`
- 树节点排序:同父下按 `sort_order` 排;移动节点时源系统可指定新 sort_order(当前工具未暴露该参数)
- 跨组织引用用 `kb://pg-{uuid}` URI(稳定 ID,不因移动/重命名变化)
- 永久删除必须先 `trash`(三态保护链,直接删返回 404,不要绕过);永久删除本身当前未暴露
- `revision_restore` 与 `restore` 名字像但语义完全不同,见上文"两个 restore 不是一回事"
- **前端 URL 只认 node_id**:`create_page` 后用它的 `node_id` 字段,绝不用页面 `id`;只有 page_id 时从树里解析节点。见上文"前端链接拼装"专节

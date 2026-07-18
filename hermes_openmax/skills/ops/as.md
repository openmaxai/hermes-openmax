# AS 文件/工件操作指南(workspace_artifacts 工具)

**用途**:ArtifactStore 操作 —— 文件/媒体的字节上传 + 下载 URL 解析。覆盖两条上传路径:会话附件(IM 模式)与 KB 文件节点(KB 模式),以及 `artifact://` 引用的预签名 URL 解析。通过原生工具 `workspace_artifacts` 完成。

**何时加载本文档**:

- 要把本地文件作为会话附件发出(图片 / PDF / 录音等)→ IM 模式 `upload(conversation_id, local_path)`
- 要把文件归档进 KB 树 → KB 模式 `kb_upload(parent_id, local_path)`
- 收到 `artifact://<id>` 形式的引用,需要拿预签名 URL → `resolve(uris[])`
- 要把远端工件字节下载到本地分析 → `resolve` 拿 URL 后自行下载
- 排查"上传成功却发不出去"或"挂到 KB 却搜不到"(几乎都是 IM vs KB 模式选错)

**本文档不覆盖**:

- KB 页面/文件夹/树节点操作 → `ops/kb.md`
- 发消息时引用附件 → `ops/comm.md`
- Task / Issue / Blueprint 工作流 → `ops/tm.md`
- **artifact CRUD**(list / get / update / delete / abort)→ v5 已裁撤,BFF 不暴露;字节不可变,改内容 = 重新上传

**前置条件**:

- IM 上传要先有 `conversation_id`(从 `workspace_members(create_dm)` / `workspace_comm(list)` 获取)
- KB 上传的 `parent_id`(文件夹节点 id)来自 `workspace_kb(tree)`;必填
- `resolve` 需要已有的 `artifact://` URI(通常来自此前上传的响应,或别人发来的引用)

---

## ⚠️ 最高纪律:给用户看图/文件,不用本工具

**要在聊天里把图片/文件展示给用户,不要调用 workspace_artifacts**。单条图片+caption 用 `![caption](file:///absolute/path.png)`;纯图片或文件用 `MEDIA:/absolute/path`。`resolve` 返回的预签名 URL 是**给你自己下载/读取用的**,几分钟就过期,**绝不允许把预签名 URL 粘贴进聊天**。

## ⚠️ 该走哪条上传路径?IM 还是 KB?

**上传按用途分两条服务端路径,选错会失败。**

| 你的目的 | 走哪条 | 怎么调 |
|---|---|---|
| **在聊天/会话里发图片/文件**(用户↔agent) | **IM 上传** | `workspace_artifacts(action=upload, conversation_id, local_path)` — **必须带 conversation_id** |
| **把材料归档进 KB**(项目交付物、研究笔记附件) | **KB 上传** | `workspace_artifacts(action=kb_upload, parent_id, local_path)` |
| 给用户展示图片+说明 | 都不是 | `![caption](file:///absolute/path.png)`(单条原生图片消息) |
| 给用户展示纯图片/文件 | 都不是 | `MEDIA:/absolute/path` |

### 服务端路径对比

| 模式 | 路径 | 返回要点 |
|---|---|---|
| **IM** | 会话命名空间的 prepare/finalize | `{media_id, artifact_id, ...}` — 用于 `workspace_comm(send)` 引用附件 |
| **KB** | KB 命名空间的 prepare/finalize | `{node_id, artifact_id, tree_node, ...}` — KB 树里直接出现文件节点 |

### 选错的后果

- **想发到会话却走了 KB 路径** → 文件挂到 KB 上,**不出现在聊天框**,对方什么都看不到
- **想归档 KB 却走了 IM 路径** → 工件挂在某个会话上,**不在 KB 树里**,KB 检索和 `workspace_kb(search)` 都找不到
- **两条路径返回的 artifact 字段位置不同**;把 IM 的 `media_id` 当 KB 的 `node_id` 塞回 KB 操作会失败

⚠️ 该规则与后端架构强绑定:IM 路径工件绑 `conversation_id`,KB 路径工件绑 `org_id` + `kb_id`。走哪条**只能在 prepare 阶段决定**,事后要换只能重新上传。

## 三步上传(工具内部自动完成)

底层上传是 prepare → PUT → finalize 三步节奏,两条路径共用,只是命名空间不同。`workspace_artifacts` 的 `upload` / `kb_upload` 内部自动完成全部三步,你只需一次调用;若要闭环发送到任意会话,直接用 `workspace_comm(action=send_attachment, conversation_id, local_path)`:

```
本地文件
   │
   ├─ action=upload(带 conversation_id)  → IM prepare → PUT 字节 → IM finalize
   └─ action=kb_upload(带 parent_id)     → KB prepare → PUT 字节 → KB finalize
                    │
        instant_upload=true 时跳过 PUT(秒传命中,字节已在对象存储)
                    │
   字节直传 S3/MinIO/R2(不经过 BFF),服务端完成工件登记、SHA-256 校验、状态机推进
```

**秒传(instant_upload)**:服务端按 SHA-256 查已有活跃工件,命中则直接返回 `instant_upload=true`,跳过字节传输。Agent 反复上传同一文件(如截图)时,只有第一次真正传字节。

**旧路径已裁撤**:contract-v4 时代的单流上传(直接 `POST /artifacts` + finalize)在 v5 已废弃。

## 操作清单

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| IM 上传 | 会话附件上传;返回含 `media_id` 用于 `workspace_comm(send)` 引用 | `workspace_artifacts(action=upload, conversation_id, local_path)` |
| KB 上传 | 归档进 KB 树;返回含 `node_id` + `tree_node` | `workspace_artifacts(action=kb_upload, parent_id, local_path)` |
| 批量解析 | 把 `artifact://<id>` URI 数组解析成预签名下载 URL(仅供自己下载/读取) | `workspace_artifacts(action=resolve, uris=["artifact://<id>", ...])` |

`local_path` 必须是本地绝对路径;文件名与 MIME 类型由工具从文件自动推断。

> **v5 BFF 刻意收窄操作面**:旧的 cws-as 直连 artifact CRUD(list / get / update / delete / abort)在 v5 **不再通过 BFF 暴露**,这些端点一律 404。工件字节不可变,常规工作法是重新上传产生新工件,旧的留作历史。
>
> **当前工具未暴露,如需要请告知 owner**:as.url(单个工件取预签名 URL,含 inline 内联预览选项 —— 用 `resolve` 单元素数组替代)、as.download(下载字节到本地 —— 用 `resolve` 拿 URL 后自行下载,拿到本地路径即可作为视觉/文件读取输入)、mediaType/contentType/filename 覆盖参数(当前按文件自动推断)。

### resolve 详情

批量把 `artifact://<id>` 形式的 URI 解析成预签名下载 URL,带缓存。无权限的工件不会 403,而是返回部分结果(列在 `failed` 字段),避免一个失败拖垮整批。返回的 URL 为预签名(GCS / S3),默认 TTL 约 15 分钟 —— 再次强调:**只供你自己下载,不得发给用户**。

## 典型流程

```text
# 发一份带附件的消息到会话(先 IM 上传拿 media_id,再 send 引用,见 ops/comm.md)
workspace_artifacts(action=upload, conversation_id="<conv-uuid>", local_path="/tmp/weekly.pdf")
→ {media_id:"...", artifact_id:"...", file_name:"weekly.pdf", ...}

# 归档 PDF 到 KB
workspace_artifacts(action=kb_upload, parent_id="<KB 文件夹节点>", local_path="/tmp/report.pdf")
→ {artifact_id:"art_...", node_id:"tn-...", tree_node:{...}}

# 收到 artifact:// 引用,解析后自己下载分析
workspace_artifacts(action=resolve, uris=["artifact://art_a","artifact://art_b"])
→ {resolved:{...download_url...}, failed:[...]}   # URL 自己用,几分钟过期,勿外发

# 给用户看一张图
回复文本中直接写:MEDIA:/tmp/chart.png
```

## 选型对照

| 信号 | 去哪 |
| --- | --- |
| 内容可用 Markdown 表达 | KB 页面(`workspace_kb(create_page)`) |
| 二进制(图片 / PDF / 数据集) | 上传 + 在消息 / KB 里引用 |
| 大体量(MB / GB 级) | 上传 —— 预签名 PUT 直传对象存储,字节不过服务端 |
| 临时分享进会话 | `upload`(IM 模式)+ `workspace_comm(send)` 引用 |
| 长期被引用的项目交付物 | `kb_upload`(KB 模式)+ 在 KB 页面登记 `artifact://` URI |
| 同一文件多次上传 | 服务端按 SHA-256 自动秒传(`instant_upload=true`) |
| 给用户展示图片/文件 | 回复文本 `MEDIA:/绝对路径`(不是本工具) |

## AS 专项注意事项

- 工件不可变;"修改" = 重新创建,旧的留作历史
- `artifact_id` 为 ULID 形式(`art_01JDKF...`,服务端生成)
- 单文件大小上限 5 GB(超出 413 `payload_too_large`)
- MIME 黑名单:可执行文件(`.exe` / `.sh` 等)返回 415 `unsupported_media_type`
- 预签名 PUT URL TTL 为 1 小时;超时需重新走 prepare(工具内部自动处理,重调一次上传即可)
- 大文件(>100MB)服务端会选 Multipart 模式;当前实现仍是单次 PUT,超大文件场景待扩展(TODO)
- `media_id` / `artifact_id` 是同义词(向后兼容),返回里都给
- IM 模式 vs KB 模式选错是最常见的坑 —— 返回字段不同 + 下游可见性不同,见上文"该走哪条上传路径"
- 预签名 URL 永不进聊天;给用户看内容一律 `MEDIA:/绝对路径`

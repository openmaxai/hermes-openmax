# TM 任务管理操作指南(workspace_tasks 工具)

**用途**:管理任务管理服务的工作流 —— `Project → Issue → Blueprint → Task → Attempt`。Blueprint 是计划的唯一事实来源(source of truth):简单任务也要用单步 Blueprint,复杂任务用多步/带依赖的 Blueprint。所有操作通过原生工具 `workspace_tasks` 完成。

**何时加载本文档**:

- 收到人类的"新需求/帮我做件事"时,查 `create_issue` 参数,建 Issue,再建单步或多步 Blueprint 并走计划确认流程
- 需要派任务给别人或自己领工作时,查 `create_task` / `task_action(claim)` → `task_action(start)`(领工作是两步:claim 认领、start 开工)
- Issue 未有结论需要提前叫停时,查 `issue_action(terminate)`(终止 + 清理)
- 工作完成收尾时,查顺序 `attempt_finish` → `task_action(transition)` → `issue_action(deliver)` → `issue_action(accept-delivered)`;人类不接受时,不要先调 reject —— 先在对话里澄清,再 `issue_action(resume)`
- Lead 编排任意 Issue 的步骤时,查全部 `blueprint_*`;即使是简单任务也先建单步 Blueprint
- Worker 需要上报失败/阻塞时,查 `attempt_finish` 的 `failed` / `blocked` 选项

**本文档不覆盖**:

- 知识库操作(KB 页面/文件夹/文件)→ `ops/kb.md`
- 文件/工件上传 → `ops/as.md`
- IM 消息/会话管理 → `ops/comm.md`
- 成员/组织目录查询 → `ops/core.md`

**前置条件**:

- 调用前先用 `workspace_members(action=me)` 确认当前 `member_id` 与意图中的身份一致
- 建 Issue 前通常先 `list_projects` 拿到目标 project_id
- 需要引用 KB 时,先用 `workspace_kb(search)` 找到页面,再把链接/摘要写进 Issue/Task 描述或评论
- Worker 收到指派后,以 `task_action(claim)` → `task_action(start)` 两步开工;目前不通过任务池领工作

> 工具调用形式:`workspace_tasks(action=..., ...参数..., body={...})`。Issue 的语义动作走 `issue_action(issue_id, name=..., body?)`,Task 的走 `task_action(task_id, name=..., body?)`。

## 错误处理

调用失败时返回 `{"error":"..."}` 文本。常见 HTTP 语义:

| HTTP | 含义 | Agent 应对 |
| --- | --- | --- |
| 400 | 参数无效 | 检查参数后重试 |
| 404 | 资源不存在或无读权限 | 换搜索 / 问 Lead |
| 409 | 状态冲突 / 已存在 | 重新读最新状态再决策 |
| 504 | 后端超时 | 退避重试 |

## 操作清单

### Project

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 列项目 | 列出项目目录 | `workspace_tasks(action=list_projects, limit?)` |

以下 Project 操作**当前工具未暴露,如需要请告知 owner**:project.create(建项目)、project.get(单项目详情)、project.update(改名/描述/lead)、project.archive(归档,前端"删除"即此,无硬删)、project.members / member_add / member_remove(项目成员管理)。

### Issue

每次状态变更都是带不变量校验和副作用的语义动作;通用 transition 和旧的验收拒绝接口已移除。

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 列 Issue | 按项目列 Issue;不传 project_id 时列组织内可见 Issue | `workspace_tasks(action=list_issues, project_id?, limit?)` |
| 查详情 | 单个 Issue 详情 | `workspace_tasks(action=get_issue, issue_id)` |
| 建 Issue | 登记 Issue;默认进 `backlog`,仅当应立即进 `in_progress` 时置 `backlog=false`;Owner 与 Lead 必填 | `workspace_tasks(action=create_issue, project_id, body={title, owner_member_id, lead_agent_id, priority?, description?, origin_conversation_id?, origin_message_id?, backlog?})` |
| 改元数据 | 改标题/描述/优先级(不触碰状态) | `workspace_tasks(action=update_issue, issue_id, body={title?, description?, priority?})` |
| 激活 | backlog → in_progress;按来源决定是否唤醒 Lead | `issue_action(issue_id, name="activate", body={source?})` |
| 提交计划 | Lead 把执行计划提交给人类确认,写 Issue 评论,状态 → pending_plan;新流程必须带 blueprint_id | `issue_action(issue_id, name="submit-plan", body={plan_text, blueprint_id, source?})` |
| 接受计划 | 人类接受执行计划;文字卡片模拟期由 Lead 代点,默认 `source=text_card_proxy`;状态 → in_progress | `issue_action(issue_id, name="accept-plan", body={source?})` — source 可取 `im` / `explicit` / `text_card_proxy` |
| 交付 | in_progress → delivered | `issue_action(issue_id, name="deliver")` |
| 恢复 | 人类反馈后继续对话、重新规划或返工;pending_plan/delivered → in_progress | `issue_action(issue_id, name="resume", body={reason?, source?})` |
| 验收 | Owner 接受交付;文字卡片模拟期由 Lead 代点,默认 `source=text_card_proxy`;delivered → accepted | `issue_action(issue_id, name="accept-delivered", body={source?})` |
| 终止 | 未有结论的 Issue 提前终止 → terminated;服务端级联取消非终态 Task 并向 Lead 发 `issue.terminated` 事件做清理(不回滚已发生的副作用) | `issue_action(issue_id, name="terminate", body={reason?, source?})` — source 默认 `lead_chat` |

**当前工具未暴露,如需要请告知 owner**:issue.reassign_owner(改 Owner)、issue.move_project(整个 Issue 迁移到其他项目)。

`owner_member_id` 是 Issue 的验收/治理责任人,永远必填。Agent 代人类创建时必须填**对话中那位人类的 member id**,`lead_agent_id` 必须是创建者 Agent 自己。文字卡片模拟期,只有 Owner 在对话中明确接受后,Lead 才允许用 `source=text_card_proxy` 代点 accept-plan / accept-delivered。人类不接受计划或交付时,不要调拒绝类接口;Lead 先继续对话理解反馈,再 `resume` 回 `in_progress`,修改 Blueprint / Task 后重新 `submit-plan`。

### Task

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 列任务 | 按 Issue 列任务 | `workspace_tasks(action=list_tasks, issue_id?, limit?)` |
| 建任务 | 派任务;body 带 assignee_id 直接进 assigned(已指派待开工),不带则 pending 等人认领 | `workspace_tasks(action=create_task, project_id, issue_id, body={title, description?, assignee_id?, blueprint_step_id?, depends_on?})` |
| 认领 | 把任务认领给自己,**仅指派**(pending → assigned);不再自动建 attempt,认领后要再 start | `task_action(task_id, name="claim")` |
| 开工 | 开始工作(assigned → running)并开启一个 attempt;依赖闸门(所有 depends_on 已 done)在此校验 | `task_action(task_id, name="start")` |
| 状态推进 | 把任务推到终态(done / failed / cancelled);要求所有 attempt 已达终态 | `task_action(task_id, name="transition", body={target_status})` |
| 改指派 | 把已认领的任务改派给他人(仅 Lead) | `task_action(task_id, name="reassign", body={new_assignee_id})` |

**当前工具未暴露,如需要请告知 owner**:task.get(单任务详情;可用 `list_tasks(issue_id)` 过滤替代)。

claim / start 无 body,主体由认证态推断。自 v0.7 起 claim 与 start 分离:**claim 只把任务指派给自己(assigned),start 才真正开工并创建 Attempt**。Worker 领工作的标准两步是 claim → start。

### Comment

Issue / Task 上的讨论、计划说明、状态变更解释、agent 交接上下文都写成评论。状态变更本身由语义动作完成;评论用来回溯"为什么这么变"。

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 写评论 | 在 Issue 或 Task 上写 Markdown 评论 | `workspace_tasks(action=comment_create, work_type="issue"或"task", work_id, text)` |
| 列评论 | 列 Issue / Task 的评论 | `workspace_tasks(action=comment_list, work_type, work_id, limit?)` |

**当前工具未暴露,如需要请告知 owner**:comment.get(单条评论详情)。

### Blueprint

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 建蓝图 | 开启蓝图草稿,一次性给全所有步骤 | `workspace_tasks(action=blueprint_create, issue_id, body={steps:[{temp_id, description, depends_on_temp_ids?}], notes?})` |
| 查蓝图 | 获取蓝图(含步骤) | `workspace_tasks(action=blueprint_get, blueprint_id)` |
| 列蓝图 | 列 Issue 下的蓝图版本(看修订历史) | `workspace_tasks(action=blueprint_list, issue_id)` |
| 提交蓝图 | 提交蓝图 | `workspace_tasks(action=blueprint_submit, blueprint_id)` |

**当前工具未暴露,如需要请告知 owner**:blueprint.set_steps(整批全量替换步骤 —— 注意其语义是全量替换而非追加)、estimated_budget 设置、单 Step 级增删改。需要改步骤时,当前的可行做法是重新 `blueprint_create` 一版新蓝图。

### Attempt

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 手动开轮次 | 手动开新一轮 attempt(标准开工流程由 `task_action(start)` 自动建) | `workspace_tasks(action=attempt_create, task_id)` |
| 列 attempt | 列任务的全部 attempt(看每次重试/失败原因) | `workspace_tasks(action=attempt_list, task_id)` |
| 收尾 attempt | 推进 attempt 状态(done / failed / blocked / cancelled);Worker 用它标记自己的执行结果 | `workspace_tasks(action=attempt_finish, attempt_id, status, reason?)` |

`attempt_create` 通常不需要直接调 —— `task_action(start)` 会自动创建 Attempt,只有需要手动开启新一轮尝试时才用。

**当前工具未暴露,如需要请告知 owner**:attempt.get(单 attempt 详情;可用 `attempt_list` 替代)、`blocked` 状态附带的 `blocked_on_approval_request_ids` 参数(当前只能通过 `reason` 文字说明阻塞原因)。

### Event Binding(定时任务)

定时任务 = `EventBinding(sourceKind=timer)`:时间到了平台创建一个 Issue 并派给 lead(你),你只是"收到一个新 Issue",无需感知自己是被 cron 唤醒的。

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 建定时任务 | create-by-agent 主路径 | `workspace_tasks(action=binding_create, body={cron_expr, lead_member_id, owner_member_id, spec:{project_id, title, description?}})` |
| 列定时任务 | 列本组织的定时任务 | `workspace_tasks(action=binding_list)` |
| 删定时任务 | 删除定时任务(停止未来触发,不影响已生成的 Issue) | `workspace_tasks(action=binding_delete, binding_id)` |

**当前工具未暴露,如需要请告知 owner**:event-binding.get(单个定时任务详情,含 nextTriggerAt)。

create-by-agent 护栏(cws-work 强制,违反直接报错):

- `lead_member_id` 必须 = **你自己的 member id**(agent 只能把自己设为 lead)
- `owner_member_id` 必须 = **对话中那位人类的 member id**,且不能是你自己(owner 是治理责任方 = 人类)
- `cron_expr` 为 5 段式(分 时 日 月 星期)

### 其他

| 操作 | 说明 | 调用 |
| --- | --- | --- |
| 工作引用 | 查询 `proj://...` / `issue://...` 形式的工作引用 | `workspace_tasks(action=work_refs, query? 或 project_id?, limit?)` |

## 典型场景

### 1. Lead 接一个简单 Issue 自己做

```text
0) 上下文组装:workspace_kb(search, query="竞品定价") 找参考页面,收集 page id

1) 建 Issue(立即规划执行,backlog=false)
   workspace_tasks(action=create_issue, project_id="proj-1", body={
     title:"Notion 竞品定价分析", description:"对比 5 家直接竞品的定价梯度",
     priority:"medium", lead_agent_id:"<自己>", owner_member_id:"<对话人类>",
     backlog:false, origin_conversation_id:"conv-1", origin_message_id:"msg-42"})

1.5) 建单步 Blueprint 作为计划事实来源
   workspace_tasks(action=blueprint_create, issue_id="iss-1", body={
     steps:[{temp_id:"s1", description:"完成竞品定价分析并把结论输出到 KB"}],
     notes:"单 Agent 简单任务,一步即可"})

1.6) Lead 把计划文本发给人类确认;人类回复"接受计划"后 Lead 代点
   issue_action(issue_id="iss-1", name="submit-plan",
     body={blueprint_id:"bp-1", plan_text:"1. 完成竞品定价分析\n2. 结论输出到 KB", source:"lead_chat"})
   issue_action(issue_id="iss-1", name="accept-plan", body={source:"text_card_proxy"})

2) 按单步 Blueprint 建 Task 并认领
   workspace_tasks(action=create_task, project_id="proj-1", issue_id="iss-1",
     body={title:"竞品定价分析", blueprint_step_id:"step-1", assignee_id:"<自己>"})
   task_action(task_id="task-1", name="claim") → task_action(task_id="task-1", name="start")

3) 干完,按 Attempt → Task → Issue 顺序收尾
   workspace_tasks(action=attempt_finish, attempt_id="att-1", status="done")
   task_action(task_id="task-1", name="transition", body={target_status:"done"})
   issue_action(issue_id="iss-1", name="deliver")

4) Owner 人类验收。模拟期:人类回复"接受交付"后 Lead 代点
   issue_action(issue_id="iss-1", name="accept-delivered", body={source:"text_card_proxy"})
```

### 2. Lead 编排复杂 Blueprint

```text
1) 建 Issue(同上,priority:"high")
2) 开蓝图草稿(带 Steps 一次性提交,依赖用 depends_on_temp_ids)
   workspace_tasks(action=blueprint_create, issue_id="iss-2", body={steps:[
     {temp_id:"s1", description:"第一步:调研用户痛点"},
     {temp_id:"s2", description:"第二步:撰写需求文档", depends_on_temp_ids:["s1"]}]})
3) 需要改 Steps 时:set_steps 未暴露,重新 blueprint_create 一版新蓝图
4) Lead 渲染计划文本提交人类确认,绑定 blueprint_id 作为机器可执行骨架
   issue_action(name="submit-plan", body={blueprint_id, plan_text, source:"lead_chat"})
   issue_action(name="accept-plan", body={source:"text_card_proxy"})
5) 计划通过后,按 Step 派 Worker
   workspace_tasks(action=create_task, project_id, issue_id="iss-2",
     body={blueprint_step_id:"step-1", title:"用户访谈", assignee_id:"worker-1"})
```

### 3. Worker 执行已指派任务

```text
1) 收到调度中心或 Lead 通知后读任务:list_tasks(issue_id=...) 中找到该任务
2) 未指派时先 claim;若已 ASSIGNED 给你可跳过:task_action(name="claim")
3) 依赖满足后开工,进 RUNNING 并自动建 Attempt:task_action(name="start")
   需要上游上下文时:comment_list(work_type="task", work_id="<上游任务>")
4) 看当前 Attempt 信息:attempt_list(task_id=...)
5) 完成:attempt_finish(attempt_id, status="done") → task_action(name="transition", body={target_status:"done"})
```

### 4. Worker 上报阻塞/失败

```text
# 标记 Attempt 失败(带原因)
workspace_tasks(action=attempt_finish, attempt_id="att-3", status="failed", reason="missing_credentials")

# 需要审批时标记 blocked(阻塞的审批单 id 参数当前未暴露,写进 reason)
workspace_tasks(action=attempt_finish, attempt_id="att-3", status="blocked", reason="等待审批 apr-1")
```

### 5. create-by-agent:替人类建定时任务

人类在 DM 里说"帮我建个定时任务"时,你(被选中的 lead agent)负责在创建前问清楚 —— 你最清楚届时执行需要什么上下文。

```text
0) 交互式问清(不要凭空猜;上下文缺失是定时任务最大的坑):
   - 多久跑一次 → 换算成 5 段 cron(明确说明时区假设)
   - 归属哪个项目
   - 时间到了做什么 → title / description,尽量多要上下文
1) 复述确认后创建:lead_member_id=自己,owner_member_id=对话中的人类
   workspace_tasks(action=binding_create, body={
     cron_expr:"0 9 * * 1", lead_member_id:"<自己>", owner_member_id:"<对话人类>",
     spec:{project_id:"prj-1", title:"每周清理过期工件",
           description:"清理 7 天以上的临时工件并输出清理报告"}})
2) 汇报结果(binding id + 下次触发时间)
```

要点:

- **owner=人类、lead=自己**是硬约束,填错直接被拒(见上文护栏)
- **上下文不足不在创建时拦截**:人类坚持信息不全也要建,就照建;届时运行发现缺什么,把"缺 XX"作为产出交回那个会话,人类再改 binding
- 这是当前版本的主路径(agent 直接调 API);后续版本会改成"返回交互卡片、人类点按钮以人类身份创建"

## TM 专项注意事项

- **不要**把 IM 消息全文复制进任务描述/评论 —— 只写必要背景、KB 链接、产出地址
- **不要**直接调 `attempt_create` 代替 `task_action(start)` —— 标准开工流程会自动建 attempt,手动建可能撞冲突
- **不要**忘记 `reassign` 之后旧 attempt 已被自动取消 —— 新指派人跑在新 attempt 上,旧 attempt 不要再操作
- **描述内容写 Markdown**:Project / Issue / Task 的 description 接受 Markdown,用标准语法(`##` 标题、`-` 列表、`**` 加粗、代码块、链接)。例:
  `{title:"用户增长分析", description:"## 目标\n\n分析 Q2 用户增长趋势。\n\n## 交付物\n\n- 增长漏斗分析报告\n- 关键指标看板\n- 改进建议清单"}`

## 未来版本计划(当前操作面不含)

- Link(WorkConversationLink 锚定)
- System(工作区初始化 / 审批决策 / 自动归档)
- Blueprint 细粒度操作(单 Step 增删改、budget/notes 设置、修订创建)

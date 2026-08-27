# 项目身份标签重构、完整迁移与验证说明

首次完成：2026-08-06（Asia/Hong_Kong）
状态更新：2026-08-27（Asia/Shanghai）

## 文档定位

本文是项目身份标签重构的最终改动说明，以当前后端代码、迁移脚本、测试结果和开发服务器部署结果为准。此前用于讨论方案的以下文档已删除，不再作为实现依据：

- project-identity-tags.md
- project-identity-tags-implementation.md
- project-identity-tags-api.md

本次改动覆盖后端身份模型、权限计算、项目生命周期、成员管理 API、邀请投影、完整数据迁移和迁移运行器；前端仓库也已完成对应 API 接入和主要界面切换。生产数据库尚未执行不可逆迁移。

## 1. 当前状态

| 范围 | 状态 | 说明 |
| --- | --- | --- |
| 后端运行时身份模型 | 已完成 | ProjectMember、TeamMember、IdentityTagPolicy、IdentityAuditEvent、IdentityOperation 已投入运行 |
| 后端权限计算 | 已完成 | 新权限服务以身份标签和团队继承为唯一运行时权限来源 |
| 成员与生命周期 API | 已完成 | 集中在 app/apis/identity.py，并接入现有 URL 注册 |
| 旧项目 workers 运行时支持 | 已移除 | 查询、搜索、权限和项目成员管理不再读取 Project.workers |
| 完整迁移 | 已完成 | 0004 回填全部身份投影并清理消费过的旧项目字段，0005 修正主体索引，0006 建立搜索投影与查询索引 |
| 前端接入 | 已完成主要切换 | 成员差分编辑、项目状态、别名、标签策略和管理页面已接入 |
| 开发服务器 | 已部署并验证 | 当前 release 为 `moeflow-20260827-214208-1b8c76e1-3282a7f5`，入口为 `http://100.90.141.40:5080/` |
| 生产数据库 | 未直接修改 | 生产归档已恢复到开发服务器并完成迁移重放；生产发布仍需独立评审 |

生产服务器本身未执行迁移。备份 `/root/moeflow-prod-20260816-104619.archive.gz` 仅在开发服务器
使用，恢复约 680,879 条文档；开发隔离库和主库均已验证 `0000` 至 `0006` 顺序执行成功，
`0004` 用时约 50 秒。测试结束后隔离库已清理，原始归档仍保留在服务器。开发数据库按授权可直接
清空后重测，无需为此保留旧数据库备份；该规则不适用于生产服务器。

## 2. 数据模型

### 2.1 ProjectMember

文件：app/models/project_member.py

ProjectMember 是项目范围内唯一的成员主体记录，支持两种且只能有一种主体：

- 注册用户：user 有值，external_id 不存在。
- 外部署名：user 不存在，external_id 由服务端生成。

主要字段和约束：

| 字段 | 语义 |
| --- | --- |
| project | 所属项目 |
| user / external_id | 注册用户或外部署名主体，二选一 |
| identity_key（数据库字段 ik） | project 加主体类型和主体 ID 的逻辑唯一键 |
| display_name | 当前项目内的展示名；注册用户创建时默认复制 User.name，之后是独立快照 |
| tags | 项目身份标签，去重并排序 |
| status | active、invited 或 removed |
| version | 成员级乐观锁版本 |
| removed_time | 软移除时间，removed 之外必须为空 |

注册用户和外部署名不能通过 display_name 合并。外部署名允许同名，绑定注册用户和合并外部署名分别使用显式 API，避免搜索结果或同名判断自动改变身份。

active 注册用户的项目标签参与项目权限计算。invited 注册用户只获得项目访问权限，不获得项目标签权限。removed 成员没有项目权限。外部署名没有 User，因此不会产生有效的项目权限。

### 2.2 TeamMember

文件：app/models/team_member.py

TeamMember 保存团队范围的注册用户身份：

- team 加 user 唯一。
- base_tag 为 creator、admin 或 member。
- tags 保存团队自定义标签。
- worker_qualifications 保存工作人员资格：raw_provider、scanner、cropper、cleaner、translator、proofreader、typesetter。
- aliases 是团队作用域别名。
- default_display_name 是「加入项目时的默认展示名」偏好（空=未设置，上限 140），
  主动加入或被邀请加入项目时默认填充为 ProjectMember.display_name（显式传入的
  展示名仍优先）。
- status 为 active 或 removed，移除不物理删除关系。
- version 用于成员配置和别名更新的并发校验。

团队 creator 不能由普通成员接口设置或覆盖。团队 admin 只能管理低于自己的成员，不能修改团队 creator；普通成员只能修改自己的团队 alias。团队 creator 和 admin 的项目权限通过权限服务实时继承，不复制写入每个项目成员的 tags。

### 2.3 标签策略、审计和幂等记录

| 模型 | 文件 | 作用 |
| --- | --- | --- |
| IdentityTagPolicy | app/models/identity_tag.py | 按团队保存 team/project 标签的覆盖和自定义定义 |
| IdentityAuditEvent | app/models/audit.py | 保存身份、标签、owner、邀请投影和项目状态的前后值及操作者 |
| IdentityOperation | app/models/identity_operation.py | 保存项目成员差分操作的状态、结果、租约和 claim token |

系统标签的初始定义在 app/models/identity_tag.py 中，团队覆盖只保存在 IdentityTagPolicy，不改变站点默认定义。审计事件使用独立集合，包含作用域、动作、目标、请求 ID、来源、前值、后值和权限来源。

### 2.4 作用域隔离

以下三个展示字段相互独立：

| 字段 | 作用域 | 修改入口 |
| --- | --- | --- |
| User.aliases | 站点 | PATCH /v1/me/aliases；管理员可修改其他用户 |
| TeamMember.aliases | 团队 | PATCH /v1/teams/{team_id}/members/{member_id}/aliases |
| TeamMember.default_display_name | 团队 | PATCH /v1/teams/{team_id}/members/{member_id}/default-display-name |
| ProjectMember.display_name | 项目 | 项目成员差分操作 |

修改任一字段不会同步另外两个字段。alias 不是登录名、唯一身份键或权限主体；同一 alias 可以被不同用户或团队使用，但不能等于当前用户的 name。默认每个作用域最多保存 10 个 alias，每个 alias 最多 64 个字符。

## 3. 权限和状态语义

### 3.1 项目标签

系统项目标签为：

creator、admin、raw_provider、scanner、cropper、cleaner、translator、proofreader、typesetter。

用户在同一项目上的多个 active 标签取权限并集。权限代码统一使用 project:NAME 形式，兼容读取旧的数字权限值，但新身份服务内部只使用稳定字符串权限。

项目权限主要包括 ACCESS、CHANGE、COMPLETE_PROJECT、MANAGE_MEMBERS、文件操作、翻译操作、校对操作、目标语言操作、邀请和成员管理等。具体默认映射由 app/models/identity_tag.py 的 PROJECT_TAG_PERMISSIONS 定义。

工作人员标签还要经过团队资格校验：

- qualified 模式下，普通目标用户必须是当前团队 active 成员，并拥有对应 worker qualification；当前操作者本人若拥有项目成员管理权限，可为自己的身份添加工作人员标签，不受该资格集合限制。
- open 模式只绕过资格集合校验，不绕过成员状态、标签可分配性、项目权限和审计规则。
- 外部署名只能使用工作人员标签，不能获得 creator、admin 或项目管理权限。

项目身份（creator/admin/member）编辑规则：

- 项目身份是「单值身份」，与多选的项目职位（工作人员标签）分开编辑，保存时合并回落
  tags 数组；前端成员管理详情拆「项目身份（单选）+ 项目职位（多选）」两个下拉。
- 只有项目创建者（owner）可以修改**他人**的项目身份（授予/移除 admin）；团队管理员
  身份下拉禁用，只能修改展示名、职位标签等非身份字段。
- 任何人都不能修改**自己的**项目身份：前端身份下拉对本人锁定，后端对
  `operator == member.user` 移除自己的身份 tag 返回 422 INVALID_IDENTITY_TAG
  （owner 移除自己的 creator 返回 409 MEMBER_ALREADY_OWNER）。
- 移除他人身份 tag 同样仅限 owner（非 owner 返回 422 INVALID_IDENTITY_TAG），
  授予 admin 本就受可分配标签（assignable）限制。

### 3.2 团队标签策略

团队管理员可通过 IdentityTagPolicy 修改团队或项目标签：

- 系统标签可以覆盖权限集合和 assignable 设置。
- 系统标签响应同时返回 source、initial_permissions 和 initial_assignable，便于前端提供还原初始权限操作；团队成员基础等级标签（creator/admin/member）的覆盖响应也带 initial_assignable=False。
- 基础等级标签（creator/admin/member）的系统覆盖与移除只允许团队 creator 执行，管理员不能借此改变团队等级体系或把受保护权限授予低级标签。
- 自定义标签只能授予对应作用域允许的权限，不能通过自定义标签获得受保护的项目完结或成员管理能力。
- 删除正在被成员使用的自定义标签会被拒绝；系统标签的删除操作表示移除团队覆盖，系统初始定义仍然存在。
- 策略更新使用 policy version 做乐观锁，冲突不会覆盖其他管理员的修改。

### 3.3 项目状态

数据库内部仍使用整数状态。为避免列表与详情接口字段类型不一致，所有项目响应统一保留整数 status 字段，同时由身份响应提供稳定的字符串 identity_status：

| identity_status | status 整数 | 语义 |
| --- | ---: | --- |
| NORMAL | 0 | 正常项目，可按标签权限编辑 |
| CLEARED | 1 | 内容已经清空，项目记录和成员身份保留 |
| COMPLETED | 5 | 已完结但内容保留，可在权限允许时恢复 |

项目状态有独立的 status_version。complete、clear、reopen 都支持 expected_status 和 expected_version，状态冲突会被拒绝。普通内容编辑权限在非 NORMAL 状态下失效：

- NORMAL 项目可以完结。
- COMPLETED 项目可以恢复到 NORMAL，也可以在团队 creator 权限下清空。
- CLEARED 项目不可通过 reopen 恢复，成员和项目记录仍保留用于审计和历史展示。
- clear 先以 clear_in_progress 锁定项目，清理文件和输出对象成功后才写入 CLEARED；对象存储失败会释放锁并返回可重试错误。

项目 owner 必须是该项目的 active 注册用户成员，并拥有 creator 标签。owner 转移使用单独接口和 owner_version 校验，不接受普通成员差分操作直接写入 owner。

## 4. 后端服务层重构

### 4.1 统一权限入口

app/services/identity_permission.py 的 IdentityPermissionService 是新运行时权限计算的唯一入口，负责：

- 根据项目成员标签和团队标签计算 PermissionSnapshot。
- 合并多个项目标签的权限来源。
- 继承团队 creator/admin 的项目权限。
- 区分 active、invited、removed 和外部署名的访问能力。
- 校验项目标签、团队标签和工作人员资格。
- 判断项目成员管理、项目生命周期、团队管理和清空项目权限。

现有项目、团队、项目集和项目搜索相关接口已接入该服务。新成员列表、搜索、权限摘要不再回退到 Project.workers。

### 4.2 项目成员服务

app/services/project_member.py 的 ProjectMemberService 负责：

- 项目成员列表、职位和关键字搜索。
- 注册用户加入、外部署名加入、恢复、更新和软移除。
- 成员标签、显示名、成员容量和版本校验。
- 外部署名绑定、显式合并和 owner 约束。
- 邀请投影和项目成员计数同步。
- 有序差分操作的幂等、部分成功和失败停止。
- 加入默认展示名：`_add_user_display_name`（显式展示名优先；未传或等于站点
  用户名时用 `team_default_display_name` = TeamMember.default_display_name，
  未设置则回退注册用户名），`_add_user` 的两个新建/重邀请分支均已接入。

每个操作只处理一个成员，服务端严格按请求数组顺序执行。前置操作成功后，后续操作失败不会回滚前置结果；客户端可根据返回的 results 和 failed 继续修正。

### 4.3 团队成员和别名服务

app/services/team_member.py 的 TeamMemberService 负责团队成员新增、恢复、配置、移除和团队 alias；app/services/user_alias.py 负责站点 alias。

成员配置写入前会校验操作者等级、目标成员状态、标签策略、工作人员资格和 expected_version。成员本人可以更新自己的团队 alias，但不能借此修改 base_tag、团队标签或工作人员资格；普通成员连自己的 tags/资格也不能通过 update 修改（管理操作只开放给 creator/admin，_can_manage_target 拦截）。creator/admin 可以更新自己的 tags 和资格，但不能修改自己的 base_tag。其他成员管理权限仍由团队等级决定：creator 管理非 creator，admin 只管理 member。项目成员快捷编辑中，拥有项目成员管理权限的操作者可以为自己添加工作人员标签，普通注册用户仍需满足团队工作人员资格。

### 4.4 邀请投影

app/services/project_invitation.py 的 ProjectInvitationAdapter 保留既有 Invitation 创建、接受、拒绝、自动加入和通知生命周期，只将结果投影到 ProjectMember：

- pending 投影为 invited。
- 已允许或直接加入投影为 active。
- 拒绝、取消等终态投影为 removed。
- 已经存在的成员关系会被复用，不重复创建成员、计数或审计事件。
- 接受/批准时若调用前不存在投影（旧 pending 邀请/申请），adapter 保留底层 join 刚写入的标签而非用空列表覆盖——`Invitation.allow` 和 `Application.allow` 在捕获不到投影时传入 tags=None，而不是 []。
- 新建/重建投影（`_ensure_projection`、`update_pending_tags` 无投影分支、
  `User.join_project`）时 display_name 默认取团队默认展示名
  （`ProjectMemberService.team_default_display_name`），显式传入的展示名仍优先。

invited 是底层加入流程的投影状态，不是新的邀请生命周期。权限服务不会使用 Invitation.role 或旧 workers 授权。

### 4.5 项目生命周期和旧运行时入口

app/services/project_lifecycle.py 统一处理 owner 转移、complete、clear 和 reopen，并记录审计。

旧的运行时成员入口已删除：

- app/apis/member.py
- app/apis/project_workers.py
- tests/api/test_project_workers_api.py

旧关系模型和 Invitation/Application 的业务记录仍可能作为底层加入流程或迁移输入存在，但新身份权限、项目成员列表、项目搜索和 workers 功能不再从旧关系或 Project.workers 读取权限数据。

## 5. API 改动

身份 API 注册在 app/apis/urls.py，具体实现集中在 app/apis/identity.py。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| PATCH | /v1/me/aliases | 当前用户整体替换站点 alias |
| PATCH | /v1/users/{user_id}/aliases | 管理员替其他用户整体替换站点 alias |
| GET | /v1/projects/{project_id}/members | 项目成员列表、状态、标签和权限摘要 |
| POST | /v1/projects/{project_id}/members/changes | 按顺序执行项目成员 add/update 差分操作 |
| POST | /v1/projects/{project_id}/members/{member_id}/bind | 外部署名绑定注册用户 |
| POST | /v1/projects/{project_id}/members/{member_id}/merge | 外部署名与已有注册用户成员显式合并 |
| POST | /v1/projects/{project_id}/owner/transfer | 转移项目 owner |
| POST | /v1/projects/{project_id}/complete | 将 NORMAL 项目标记为 COMPLETED |
| POST | /v1/projects/{project_id}/clear | 清理内容并将项目标记为 CLEARED |
| POST | /v1/projects/{project_id}/reopen | 将 COMPLETED 项目恢复为 NORMAL |
| GET/POST | /v1/teams/{team_id}/members | 团队成员列表和新增/恢复 |
| PATCH/DELETE | /v1/teams/{team_id}/members/{member_id} | 团队成员配置和软移除 |
| PATCH | /v1/teams/{team_id}/members/{member_id}/aliases | 更新团队 alias |
| PATCH | /v1/teams/{team_id}/members/{member_id}/default-display-name | 更新加入项目的默认展示名（self 或可管理目标的管理者，须 expected_version） |
| GET/PATCH | /v1/teams/{team_id}/identity-tag-policy | 读取或更新团队标签策略 |

### 5.1 项目成员差分请求

请求体必须包含 operations 数组。每一项必须有唯一 operation_id，并使用 action=add 或 action=update：

~~~json
{
  "operations": [
    {
      "operation_id": "local-operation-1",
      "action": "add",
      "user_id": "registered-user-id",
      "tags": ["translator"]
    },
    {
      "operation_id": "local-operation-2",
      "action": "update",
      "member_id": "project-member-id",
      "expected_member_version": 3,
      "changes": {
        "tags": ["translator", "proofreader"]
      }
    }
  ]
}
~~~

add 可以使用 user_id 或 display_name。display_name 仅用于创建新的外部署名，不是身份键。update 只允许修改 display_name、tags 或 status=removed；恢复 removed 成员使用 add 加 member_id、expected_member_version 及主体校验字段，恢复路径与新增一样执行容量检查。

operation_id 由前端按「project_id + action + 成员主体键」稳定生成（不携带时间戳/随机数），同一保存重试会命中同一幂等记录；外部署名的稳定 external_id 也由该 operation_id 派生，不稳定的 id 会在网络重试时产生重复外部署名。operation_id 在主体基之外追加**操作内容指纹**（tags 全集/displayName/status）：相同内容的重试仍命中同一记录，不同内容（如为同一成员追加第二个职位 tag、切换身份）得到新 id 并正常执行，避免内容变更被幂等层误重放。

响应包含：

- results：已成功完成的操作和新的成员版本。
- failed：第一项失败操作的错误码、消息和 HTTP 状态。
- stopped：失败后是否停止后续操作。

服务端会先验证整个 operations 外层结构，再逐项执行。重复的 operation_id 会被拒绝；已成功的 operation_id 重试直接返回持久化结果，不重复产生成员、邀请、计数或审计副作用。

### 5.2 绑定、合并和生命周期请求

- bind 要求 expected_version 和 user_id，目标必须是 active 外部署名，绑定用户必须是当前团队 active 成员。
- merge 要求 target_member_id、expected_source_version、expected_target_version；display_name 可选（缺省沿用目标显示名），tags 必填。源必须是 active 外部署名，目标必须是 active 或 invited 注册用户成员，且目标必须是当前团队 active 成员（额外要求）；合并不得剥离当前 owner 的 creator 标签（剥离会被 MemberAlreadyOwnerError 拒绝）。两条成员记录任一版本冲突都会回滚已经完成的另一侧写入（best-effort 回滚，失败写入日志而非静默吞掉）。
- owner/transfer 使用 new_owner_user_id，可选 expected_owner_user_id 和 expected_version；new_owner_user_id 必须是合法 ObjectId 且解析为存在的用户，畸形 id 返回显式业务错误。
- complete、clear、reopen 可传 expected_status 和 expected_version。
- X-Request-ID 或 Idempotency-Key 会作为请求/幂等关联信息写入审计和操作记录。

无权限、状态冲突、版本冲突、容量不足、资格不满足、标签非法和对象不存在均通过显式业务错误返回，不以未处理异常形式暴露为 500。

## 6. 完整迁移

### 6.1 迁移版本

| 版本 | 文件 | 内容 | 回滚 |
| --- | --- | --- | --- |
| 0004 | app/migrations/versions/m0004_identity_members.py | 读取旧关系、角色、workers、邀请和申请，回填新身份集合，迁移项目状态并清理旧字段 | 不可逆 |
| 0005 | app/migrations/versions/m0005_identity_member_index_options.py | 清除 external_id 的 null 占位并重建主体唯一索引 | 不可逆 |
| 0006 | app/migrations/versions/m0006_search_projections.py | 为成员搜索回填规范化投影并建立搜索所需索引 | 不可逆 |

0004 使用原始 PyMongo 文档读取旧 schema，因此不会依赖已经切换后的 MongoEngine 运行时模型。主要输入包括：

- project_user_relation、team_user_relation。
- project_role、team_role。
- project.w（旧 workers）。
- invitation、application。
- project、team、user。

### 6.2 回填和合并规则

迁移按 project 加 user、team 加 user 的稳定主体键合并重复关系：

- 重复项目关系合并标签，重复团队关系按 creator > admin > member 选择最高 base_tag。
- 旧角色映射到新项目标签：creator、admin、proofreader、translator、typesetter 等；coordinator 映射为 proofreader，supporter 映射为 translator。
- 旧 relation 的 m_t 是历史关系标注，不直接变成新标签，只写入迁移报告。
- workers 中的 provider、图源、scan、扫图、修图、翻译、校对、嵌字等值映射为 raw_provider、scanner、cleaner、translator、proofreader、typesetter 等外部署名标签。
- 外部署名按 project 和规范化展示名生成稳定 UUID，重复运行不会新建第二条主体。
- 用户名用于初始化注册用户 ProjectMember.display_name；外部署名保留原项目展示名。
- 成员计数按新集合中的 active 成员重新计算，不沿用旧缓存；verify 会校验 m_uc 与 active 成员数一致。
- 主体无效的残缺文档（无 project/team 作用域，或 user/external_id 缺失）不再被物理删除：不可逆迁移里删除即永久丢失，改为保留文档并在报告中记录 invalid_identity_member 问题（它们不满足新唯一索引的 partial filter，不构成唯一性漏洞）。

旧加入流程的投影规则如下：

- 现有 active 关系优先作为 active ProjectMember。
- pending invitation 投影为 invited。
- accepted 但没有旧关系的加入事件可生成无标签 active 成员。
- pending application 不生成可访问成员，因为旧应用流程尚未授予访问权限。
- 拒绝或取消的加入流程保留为 removed 逻辑成员，后续重新加入时复用主体。

### 6.3 项目状态和旧字段清理

旧项目状态映射为：

- 0 保持 NORMAL。
- 1 保持 CLEARED。
- 5 保持 COMPLETED。
- 2（计划完结）和 3（计划删除）统一回到 NORMAL，并在报告记录待人工复核的问题；迁移不会执行破坏性删除。
- 未知状态回到 NORMAL，并记录 status issue。

迁移完成后：

- 删除项目旧 workers 字段 w。
- 删除旧计划完结字段 pft、旧计划清空/删除字段 pdt 和旧完结字段 ft。
- 保留并校验新状态 st、status_version stv、owner_version ouv、completed_time ctm 等新字段。
- owner 缺失时优先自动兜底：把 `ou` 绑定到项目所在团队的团队创建者（从 `team_user_relation`
  中 creator 角色推导），团队也无创建者时退回网站创建者（`_id` 最早的用户），并补写一条带
  `creator` 标签的 `project_member` 行；兜底不增加 `owner_issue_count`，verify 照常通过。
  审计：报告 issue code `owner_bound_to_team_creator` / `owner_bound_to_site_creator`
  （归入 owner-issues.jsonl）。仅当兜底不可行（如站点无用户）时，才与冲突、无效、无法修复
  的情况一样写入报告并使 verify 失败，阻止迁移被记录为成功。
- verify 同时校验：display_name 不超过运行时上限 140、m_uc 与 active 成员计数一致、主体唯一键 ik 正确、版本/时间字段类型合法；对有自定义标签语义的未知标签保持宽容（与运行时惰性语义一致），不因此误阻断。
- 为 ProjectMember、TeamMember、IdentityTagPolicy 建立或补齐唯一和查询索引。

旧关系集合不会被迁移脚本粗暴删除，因为底层 Invitation/Application 生命周期和审计仍需要其业务记录。它们不再是新身份权限和成员查询的数据源；如果未来要归档旧集合，应作为独立的数据保留/清理方案处理。

### 6.4 迁移工件

0004 在 IDENTITY_MIGRATION_ARTIFACT_DIR 指定的目录下生成：

~~~text
<root>/identity-migration/<batch_id>/
  project-members.jsonl
  team-members.jsonl
  owner-issues.jsonl
  role-issues.jsonl
  permission-diff.jsonl
  workers-audit.jsonl
  relation-merge.jsonl
  manifest.json
  checksums.json
~~~

每行包含 batch_id、migration_version、mapping_version、迁移结果、来源 ID、迁移前后摘要和问题信息。工件不保存邮箱、密码、token 等敏感字段；workers 原始结构仅保存摘要哈希和长度。

manifest 记录固定的 7 个 JSONL 文件、各文件数量、摘要和来源报告集合。checksums 对 manifest 和 7 个 JSONL 文件计算 SHA-256。固定 batch_id 重试时，如果完整工件和校验和仍然有效，迁移会保留原证据，不会因旧字段已清理而覆盖为空报告。

### 6.5 迁移运行器保护

app/migrations/runner.py 现在提供：

- 对迁移源码进行 AST 语义校验和，格式化变化不会改变已记录校验。
- 迁移记录按版本和 checksum 校验，已应用迁移被修改会直接阻断；0004 之前的旧记录若无法用当前算法复现，会告警但仍接受（历史记录没有规范 checksum 表示，静默放行会掩盖修改痕迹）。
- 严格顺序执行，禁止跳过较早版本运行后续 AUTO 迁移。
- 数据库迁移锁和 30 分钟租约；租约在单条迁移间隙续期，若续期时发现租约已被其他进程接管（matched_count 为 0）直接报错停止，避免两个 runner 并发执行同一批 up()；单条迁移耗时超过租约 60% 时记录告警，提示增大 LOCK_LEASE 或拆分迁移。
- 失败停止，未完成迁移不会被记录为已应用。
- 不可逆迁移显式拒绝 down；生产回滚依赖数据库和对象存储快照。

0005 还兼容旧测试驱动和旧部署生成的索引：先移除 external_id: null，再删除同名或同键旧索引，最后建立带 partialFilterExpression 的规范索引；mongomock 使用等价 sparse fallback。

## 7. 前端接入范围

前端位于独立仓库 moeflow-frontend，本次后端改动对应的接入点包括：

- apis/member.ts：成员列表、成员差分、绑定、合并和别名请求。
- utils/projectMembers.ts：按身份主体保存草稿、计算成员变动和标签变动、生成后端 operations。
- utils/teamMembers.ts：团队成员状态和管理权限判断。
- utils/identityTags.ts：标签策略响应、系统覆盖和自定义标签差分。
- utils/identityLabels.ts：系统身份标签名称和项目/团队权限本地化显示。
- utils/projectSearch.ts：项目集/项目工作人员搜索使用新成员字段。
- components/shared/MemberStats.tsx：保留快捷成员编辑入口，支持按职位加载和编辑项目成员。
- components/team/IdentityTagPolicy.tsx：系统标签覆盖、还原初始权限和自定义标签删除。
- 项目、团队成员管理页面：使用新成员状态、版本和权限摘要。
- apis/member.ts：`TeamMember` 补 `defaultDisplayName`；`shared-form/MemberList.tsx`
  团队视图新增「加入项目的默认展示名」输入框（保存走
  `updateTeamMemberDefaultDisplayName`，manager 或本人可编辑）。
- components/shared/EditWorkers.tsx：快捷编辑支持「简略模式/完整模式」双面板
  （全部职位同时显示、每行独立搜索词、focus 触发搜索、空输入推荐已加入成员、
  不做展示名快捷编辑）；面板模式按设备记住（`localStorage.editWorkersMode`）。

快捷编辑的前端约定是保留原有按职位分配的操作方式：同一注册用户选择多个职位时，在内存中形成同一成员的多个 tags；确认时以前端保存的原始快照和当前草稿做差分，只发送新增、标签修改和移除操作。搜索注册用户和项目内已有成员不会因搜索结果自动绑定外部署名，绑定和合并必须走显式管理操作。

项目卡片和列表使用 NORMAL、COMPLETED、CLEARED 三种状态；已完结筛选同时包含 COMPLETED 和 CLEARED。COMPLETED 可进入只读查看并恢复，CLEARED 只保留历史展示，不能进入可用设置。快捷编辑和管理页面继续由后端权限摘要控制可执行动作，清空操作只对团队 creator 暴露。

## 8. 测试和验证

### 8.1 本地后端专项

执行命令：

~~~text
py -3.12 -m pytest -q tests/model/test_identity_models.py tests/model/test_identity_permission.py tests/model/test_identity_services.py tests/api/test_identity_api.py tests/base/test_migrations.py
~~~

当前 Windows 本地环境已切换到 Python 3.12.5，依赖锁文件中的 `itsdangerous==2.2.0` 与
应用可正常导入；专项用例可使用上面的 `py -3.12` 命令运行。完整回归仍以开发服务器容器为准，
因为该服务器只有约 960 MiB 内存，必须按 §8.3 的分组方式关闭 coverage 后执行。

专项覆盖：

- identity 模型字段、状态、规范化和唯一索引。
- 多标签权限并集、团队 creator/admin 继承、工作人员资格和 open 模式。
- 成员新增、更新、软移除、恢复、容量和版本冲突。
- 差分操作顺序、部分成功、失败停止、重复请求和过期租约抢占。
- 外部署名区分、绑定、合并、崩溃恢复和审计去重。
- 邀请/申请投影、加入流程状态和个人项目范围。
- 团队默认展示名：本人/管理者修改、trim 与清空、越权、版本冲突、超长、审计，
  以及主动加入/被邀请/重邀请时的默认填充（显式名优先、隐式站点名命中默认）。
- owner 转移、NORMAL/COMPLETED/CLEARED 生命周期和清理失败重试。
- owner 缺失兜底绑定（团队创建者/网站创建者）、兜底补 creator 成员与无兜底时的 owner_missing 保护。
- 标签策略覆盖、还原、删除保护、作用域隔离和搜索分页。
- 0004/0005/0006 回填、状态规范化、旧字段清理、搜索投影、迁移工件和 checksum。
- 修复回归：普通成员越权、畸形 id 业务错误、owner 标签剥离保护、容量恢复、移除成员权限裁剪、稳定 operation_id、租约丢失报错、无效主体文档保留、verify 增强断言等，见 docs/identity-refactor-review-and-fixes.md。

### 8.2 前端和开发服务器

| 验证项 | 结果 |
| --- | --- |
| 前端 Jest（身份专项历史记录） | 18 suites / 110 tests passed（该轮记录） |
| 前端生产构建 | 通过（tsc --noEmit 0 错误） |
| 容器内身份专项（历史记录） | tests/api/test_identity_api.py 32/32、tests/model/test_identity_services.py 40/40 |
| 开发服务器 OSS 回归 | 5 项通过（历史记录） |
| 开发服务器迁移/性能回归 | 23 项通过（历史记录） |
| 开发服务器低内存全量回归 | `475 passed`，4 个 subtests；按目录分组、串行、禁用 coverage |
| 开发服务器迁移 | `0000` 至 `0006` 按顺序成功 |
| 真实生产归档恢复后的迁移重测 | `0000` 至 `0006` 全量 APPLIED，`0004` 用时约 50 秒，约 680,879 条文档 |
| applications、members、members/changes API | 已验证，非团队成员申请返回预期业务 400，而非 500 |
| /api 代理和容器健康检查 | 通过（/api/ping 200） |
| Celery 缩略图路由 | thumbnail_route=output |

开发服务器入口：

~~~text
http://100.90.141.40:5080/
~~~

前端生产部署使用工作区根目录的 `moeflow.ps1 -Action Deploy -AllowDirty`，当前开发服务器 release 为：

~~~text
moeflow-20260827-214208-1b8c76e1-3282a7f5
~~~

### 8.3 修复回归的运行方式

Windows 本地可以使用系统 Python 3.12.5 做导入、编译和专项测试；开发服务器上的完整回归必须
在容器内分批执行。统一的低内存参数为：

~~~text
pytest -q -o addopts= --no-cov -p no:cacheprovider --disable-warnings --maxfail=1
~~~

按 `tests/base`、`tests/api`、`tests/model`、`tests/other` + `tests/tasks` 顺序串行执行，
每组结束释放测试进程和临时容器，并用 `docker stats` 监控内存。`moeflow.ps1 -Action Verify`
用于部署验收，`-Action Test` 只覆盖无应用数据写入的安全测试门，不等同于完整 pytest。
前端测试在 `moeflow-frontend` 仓库执行 `npm test`。

## 9. 已知限制和上线要求

1. 0004 和 0005 不可逆。生产执行前必须备份 MongoDB、对象存储和迁移工件目录，并确认可恢复演练结果；开发数据库重测可按授权直接清空。
2. 生产环境必须把 /app/artifacts 映射到受限且持久的存储，不能把迁移工件直接写入短生命周期容器层
   （实测：开发机未挂载时，重测批次写入容器层，需手动 `docker cp` 拷出持久化，容器重建即丢）。
3. 已应用迁移版本不可直接改写。`manage.py migrate`（含部署 init 门禁）会校验已应用记录的
   checksum，修改 0004 行为后旧记录直接报 `Applied migration 0004 has been modified` 并阻断
   部署；dev 重测需先整体 drop 目标库（仅 `--drop` 恢复不会移除归档外的残留迁移记录）再恢复
   数据源重新迁移。若生产目标库存在不同 checksum 的 0004 或 0005，应先备份并新增前向修复迁移。
4. Python 3.12、Pillow 12 和更新后的存储 SDK 已在开发服务器完成导入、迁移和完整回归验证；生产发布仍需按实际对象存储、RabbitMQ 和 Celery 配置做验收。
5. 前端 typecheck 不再被阻塞：原 `AdminImageSafeCheck.tsx` 引用的图片安全检测功能已由后端
   有意移除（commit c577fe3），页面从未接入路由；经确认后已删除该孤儿页面，
   `tsc --noEmit` 全量通过（详见 docs/identity-refactor-review-and-fixes.md §8）。
6. 前端最终视觉验收仍以开发服务器实物为准，后端文档只记录接口和状态语义，不替代 UI 交互验收。

## 10. 文件索引

### 后端核心实现

- app/models/project_member.py
- app/models/team_member.py
- app/models/identity_tag.py
- app/models/identity_operation.py
- app/models/audit.py
- app/services/identity_permission.py
- app/services/project_member.py
- app/services/team_member.py
- app/services/project_invitation.py
- app/services/project_lifecycle.py
- app/services/user_alias.py
- app/apis/identity.py
- app/apis/urls.py

### 迁移和运行器

- app/migrations/runner.py
- app/migrations/versions/m0004_identity_members.py
- app/migrations/versions/m0005_identity_member_index_options.py
- app/migrations/versions/m0006_search_projections.py
- artifacts/identity-migration/<batch_id>/

### 审查发现与修复记录

- docs/identity-refactor-review-and-fixes.md：本次前后端代码审查的全部发现、修复决策与对应测试。

### 专项测试

- tests/model/test_identity_models.py
- tests/model/test_identity_permission.py
- tests/model/test_identity_services.py
- tests/api/test_identity_api.py
- tests/base/test_migrations.py

### 前端对应实现

- ../../moeflow-frontend/src/apis/member.ts
- ../../moeflow-frontend/src/utils/projectMembers.ts
- ../../moeflow-frontend/src/utils/teamMembers.ts
- ../../moeflow-frontend/src/utils/identityTags.ts
- ../../moeflow-frontend/src/components/shared/MemberStats.tsx
- ../../moeflow-frontend/src/components/team/IdentityTagPolicy.tsx

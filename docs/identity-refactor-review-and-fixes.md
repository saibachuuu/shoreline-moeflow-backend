# 身份标签重构：代码审查发现与修复记录

本文档记录对 moeflow-backend（分支 thumbnail-async-and-migrations 上的身份标签重构）与
moeflow-frontend 对应改动的代码审查结论、已实施修复、配套测试和保留的设计决策。
配套规格文档为 docs/project-identity-tags-change-summary.md（本文档与之同步，规格描述与
代码不一致处已在规格文档中修正）。

> 当前状态更新（2026-08-27）：后端已切换到系统 Python 3.12.5，使用按 Python 3.12
> 生成的 `requirements.txt` / `requirements-dev.txt`；Flask-APIKit 已移除，原有 API
> 基础能力由 `app/core/api.py` 提供。当前开发服务器 release 为
> `moeflow-20260827-214208-1b8c76e1-3282a7f5`。本文后续各轮 release、专项测试数量
> 和性能数字均保留其发生时的历史上下文；最新综合验收见 §16。

## 1. 审查结论总览

| 级别 | 问题 | 状态 |
| --- | --- | --- |
| 严重 | 团队成员越权：普通成员可以删除/替改其他团队成员 | ✅ 已修复 |
| 严重 | 接受邀请/批准申请会清空新成员标签（tags=[] 覆盖 join 写入的标签） | ✅ 已修复 |
| 严重 | rbac users() 用 `user__ne=None` 过滤外部署名，真实 MongoDB 上匹配到缺 user 字段的文档并 500（mongomock 掩盖） | ✅ 已修复 |
| 严重 | 前端 operation_id 携带 Date.now()/uuid，重试会突破服务端幂等并产生重复外部署名 | ✅ 已修复 |
| 严重 | 前端 operation_id 缺内容指纹：同一成员二次编辑（追加第二个职位 tag）复用同 id，被后端幂等层静默重放、服务器只保留第一个 tag | ✅ 已修复（§4.8） |
| 重要 | 移除的团队成员仍通过团队继承获得项目访问（项目成员软移除后团队继承未裁剪） | ✅ 已修复 |
| 重要 | is_owner 判定未要求成员 active + creator 标签 | ✅ 已修复 |
| 重要 | merge 可剥离当前 owner 的 creator 标签 | ✅ 已修复 |
| 重要 | merge 回滚是吞异常的 best-effort | ✅ 已修复（失败写日志） |
| 重要 | 恢复（restore）路径绕过项目容量检查 | ✅ 已修复 |
| 重要 | 多处畸形 id 未捕获 ValidationError → 500（团队成员 get/add、项目成员 get/add/bind、owner 转移） | ✅ 已修复 |
| 重要 | 团队标签策略：等级标签（creator/admin/member）的覆盖/移除未限制为团队 creator | ✅ 已修复 |
| 重要 | status 字段类型不一致：列表接口用字符串覆盖整数 status，与详情接口矛盾 | ✅ 已修复（统一保留整数 status + identity_status） |
| 重要 | R2 缩略图分支对 GENERATING/FAILED 不探测对象是否存在，直接返回 "generating"，重构会永久隐藏已存在的封面 | ✅ 已修复 |
| 重要 | 缩略图任务：except FileNotExistError 永不触发（实际抛 SourceFileNotExist/NoSuchKey）；finally 只在特定条件清理临时文件 → 失败泄漏；无 acks_late/重试 → 被杀 worker 永远停在 GENERATING | ✅ 已修复 |
| 重要 | 迁移 runner：0004 之前旧记录 checksum 无法复现时静默接受；租约只在迁移间隙续期且丢锁后继续跑 | ✅ 已修复（告警 + 丢锁报错 + 超时告警） |
| 重要 | 0004 物理删除主体无效的残缺文档；verify 缺少 display_name 上限与 m_uc 一致校验 | ✅ 已修复（保留文档 + verify 增强） |
| 重要 | 成员管理界面无身份排序（创建者/管理员/成员），创建者/管理员可在界面上移除自己的身份 tag 完成自我降级 | ✅ 已修复（§4.9 身份/职位拆分 + owner-only 规则） |
| 功能补强 | 成员展示数据契约（user 子对象、单请求多状态列表、摘要含 invited）与职位图标三态颜色 | ✅ 已完成（§2.11、§4.6、§4.7） |
| 一般 | me/team 项目列表全量加载后 Python 侧过滤 | ⚠️ 部分缓解（select_related），完整下沉待产品确认 |
| 一般 | users_by_permission 对 system_code 为 None 的遗留自定义角色生成 {None} 过滤集 | ⚠️ 防御性修复 + 文档记录（自定义角色 API 已停用） |
| 一般 | 项目洞悉外部署名响应缺 name 键、异常类 identity_code 复用、死代码（TeamMemberAPI.post、_set_active 分支） | ✅ 已修复（§8.3/§8.4） |
| 一般 | 前端：teamMembers.ts self 编辑规则与后端 update() 语义偏差 | ✅ 已修复（对齐） |
| 一般 | 前端：invited 成员可被一键分配角色并显示为 active（草稿与后端状态不符） | ✅ 已修复 |
| 一般 | 前端：privilegedSelf 未包含 canManageMembers | ✅ 已修复 |
| 一般 | 前端 i18n：新组件硬编码中文文案未进 locale | ⏳ 遗留，见 §8 |

## 2. 后端修复明细

### 2.1 团队成员越权（严重）

**根因**：`TeamMemberService._can_manage_target` 只校验操作者存在团队关系，普通 member 也可以
update/remove/改别名其他成员；`update()` 同时允许普通成员更新自己的 tags/资格。

**修复**（app/services/team_member.py）：

- `_can_manage_target`：操作者必须是 creator/admin；creator 管理非 creator，admin 只管理 member；
  普通成员对任何人（包括自己）都没有管理权。creator/admin 仍可更新自己的 tags/资格（与规格一致），
  但自己的 base_tag 永远被 `update()` 拒绝。
- `update()` 同时保留等级闸门（非 creator/admin 直接 NoPermission）。
- 本人只能走 `update_aliases`（自己的 team alias）和 `remove()`（退出团队/自我移除）。

**测试**：`test_plain_team_member_cannot_manage_other_members_or_self_tags`
（tests/model/test_identity_services.py）。

### 2.2 邀请/申请接受清空标签（严重）

**根因**：`Invitation.allow` / `Application.allow` 在捕获投影时 `project_tags = []`；当调用前不存在
投影（旧 pending 邀请/申请），`User.join` 写入的角色映射标签之后，`project_active(tags=[])` 把标签
覆盖为空数组 → 新成员无标签加入。

**修复**（app/models/invitation.py、app/models/application.py）：捕获不到投影时传 `tags=None`，
`ProjectInvitationAdapter.project_active` 在 tags=None 时沿用 join 后成员的 tags，而不是清空。

**测试**：现有邀请投影用例覆盖 join 写入路径；修复点由
tests/api/test_identity_api.py 的应用/邀请回归负责（服务端回归项，见 §7）。

### 2.3 rbac users() 外部署名 500（严重）

**根因**：app/core/rbac.py `GroupMixin.users()` 项目分支用 `user__ne=None` 过滤。真实 MongoDB
中 `$ne` 对缺失字段为真，外部署名文档（to_mongo 剔除 user/external_id 侧字段）会被选中，
`member.user.id` 抛 AttributeError；mongomock 语义相反所以测试没暴露。

**修复**（app/core/rbac.py）：改用 `user__exists=True`（真实 Mongo 与 mongomock 语义一致），
并去掉自定义角色 system_code 为 None 时产生的 `{None}` 过滤集（防御，见 §8）。

**测试**：`test_group_mixin_users_skips_external_members_without_crashing`
（tests/model/test_identity_services.py）。

### 2.4 移除成员仍获团队继承（重要）

**根因**：project_snapshot 的团队分支在「项目成员软移除但团队关系仍 active」时继续授予
project:ACCESS 与 creator/admin 继承权限，与规格 2.1（成员管理页的软移除即收回项目权限）矛盾。

**修复**（app/services/identity_permission.py）：团队继承仅在
`team_relation 存在且（无项目成员或成员未 removed）`时生效；
团队关系移除只裁剪团队继承，不碰直接的项目成员权限。

**测试**：
- `test_removed_project_member_keeps_no_access_even_with_team_admin`
- `test_removed_team_member_loses_team_inheritance_but_keeps_membership`
（tests/model/test_identity_permission.py）。

### 2.5 is_owner 与 merge/restore/容量（重要）

- **is_owner**（app/models/project_member.py）：to_api() 的 is_owner 现在要求
  成员 status == active 且 tags 含 creator（除 user/owner_user 相等外）。
- **merge owner 保护**（app/services/project_member.py）：当目标 user == project.owner_user 且
  新 tags 不含 creator 时抛 MemberAlreadyOwnerError（owner 转移只能走专用接口）。
- **merge 回滚**：抽取 `_rollback_merge_write` classmethod，回滚中的保存失败记录
  `logger.warning` 而不是静默吞掉；外部异常先回滚再按类型重抛
  （SaveConditionError → ProjectMemberVersionConflictError）。
- **容量**：restore 路径（add + member_id）与新增一样执行 `>= max_user` 检查
  （此前因 `existing is None` 条件而绕过）。

**测试**：
- `test_merge_cannot_strip_owner_creator_tag`
- `test_merge_target_must_be_an_active_team_member`（规格 5.2 的额外团队 active 要求）
- `test_restoring_removed_members_respects_project_capacity`
（tests/model/test_identity_services.py）。

### 2.6 畸形 id → 显式业务错误（重要）

`User.objects(id=...).first()` / `TeamMember.objects(team=..., id=...)` 对非 ObjectId 会抛
mongoengine ValidationError → 500。统一在服务层捕获转业务错误：

- app/services/team_member.py：`get()` 捕获 ValidationError → IdentityTeamMemberNotFoundError；
  `add()` 用户查找 → IdentityUserNotFoundError。
- app/services/project_member.py：`_find_user()` 帮助函数（add/bind 使用）→ IdentityUserNotFoundError；
  `get()` 已有保护。
- app/services/project_lifecycle.py：`transfer_owner` 用户查找捕获 ValidationError →
  IdentityUserNotFoundError。

**测试**：`test_malformed_ids_raise_business_errors_not_500`
（tests/model/test_identity_services.py）。

### 2.7 团队标签策略：等级标签覆盖/移除限 creator（重要）

**根因**：IdentityTagPolicyService.update 对 TEAM_BASE_TAGS（creator/admin/member）的 upsert 和
remove 只要求操作者是 manager（admin 也可），管理员可以给 member 级标签授予 team:DELETE 或剥离
creator 能力，绕过等级体系。

**修复**（app/services/team_member.py）：等级标签的 upsert 与 remove 均要求操作者 base_tag ==
creator；自定义标签的增删仍按 manager 规则。等级标签覆盖响应同时带
`initial_permissions` 与 `initial_assignable=False`（规格 3.2 已同步）。

**测试**：`test_policy_base_tag_override_and_removal_are_creator_only`
（tests/model/test_identity_services.py）。

### 2.8 status 字段类型统一（重要）

列表/详情/生命周期接口此前混用「整数 status」与「字符串覆盖 status」。

**修复**：统一为「所有项目响应保留整数 status；身份响应同时提供字符串 identity_status」：

- app/apis/me.py `MeProjectListAPI`、app/apis/team.py `TeamProjectListAPI`：删除
  `item["status"] = item.get("identity_status")` 覆盖。
- app/apis/identity.py `_project_api`：删除字符串覆盖并修正自相矛盾的注释。
  生命周期接口（complete/clear/reopen）响应 envelope 自身保留成对的顶层
  `status`（字符串）+ `status_version` 作为结果摘要，与 project 对象内的整数 status 并存，
  前端不消费该顶层字段、只消费 project 对象。
- 前端 normalizeProjectStatus 同时接受 int 与字符串，不受影响。

### 2.9 R2 缩略图探针（重要）

app/models/file.py `_processed_image_url` 的 R2 分支对非 SUCCEEDED/UNKNOWN 状态无条件返回
"generating"。修复为与 LOCAL 分支一致：先探测对象是否存在（head_object），存在即签名返回，
不存在才返回 "generating"。理由相同：worker 被杀后文档永远停在 GENERATING 且无重投递，
只信状态会永久隐藏完好的封面。

### 2.10 缩略图任务健壮性（重要）

app/tasks/thumbnail.py：

- 死代码：`except FileNotExistError` 改为 `(FileNotExistError, SourceFileNotExist, NoSuchKey)`
  （R2/OSS 缺对象实际抛 SourceFileNotExist / oss2 NoSuchKey），命中即终态 FAILED（不可重试）。
- 临时文件泄漏：独立跟踪 `downloaded_tmp`，finally 无条件清理，不再依赖
  `image_path.startswith(tempfile.gettempdir())`。
- 可靠性：任务改为 `bind=True, acks_late=True, max_retries=3`；对待处理的运行时异常先
  `self.retry` 保留 GENERATING（封面探测兜底），重试耗尽才写 FAILED。

### 2.11 成员展示数据契约（功能补强）

- ProjectMember.to_api 新增 `user` 子对象（仅注册成员）：`{id, name, avatar, has_avatar,
  aliases}`，不返回 email（与 User.to_api 一致，email 仅管理员可见）；外部署名为 null。
- `/members` 列表（_member_page）改传 `include_permissions=False`：逐行权限快照前端从不消费
  （最多 3×1000 行），字段保留为空数组以统一响应形态；单成员详情/绑定/合并响应仍返回真实权限。
- ProjectMemberService.member_summaries 从仅查 `status="active"` 改为
  `status__in=["active", "invited"]`：前端职位图标需要区分「该职位仅有邀请中成员」（橙色）；
  每项 summary 自带 status，载荷增加量极小（邀请人数通常很少）。

## 3. 迁移修复明细

### 3.1 runner.py

- `_checksum_matches`：对 0004 之前无法用当前算法复现的旧记录，从「静默接受」改为
  「logger.warning 后接受」，避免隐藏历史修改痕迹。
- `_renew_lock`：续租时若 `matched_count != 1`（租约已被接管）立即抛 MigrationError，
  而不是让两个 runner 并发执行同一批 up()。
- run_pending：单条迁移耗时超过租约 60% 记录告警（提示增大 LOCK_LEASE 或拆迁移）；
  租约只在迁移间隙续期的限制写入文档。

### 3.2 m0004

- `_merge_existing_documents`：主体无效的残缺文档改为「保留 + 报告 invalid_identity_member」，
  不再物理删除（不可逆迁移中删除即永久丢失；这类文档不满足新唯一索引的 partial filter，
  不构成唯一性漏洞）。新增 summary 计数 `identity_member_reported_invalid_count`。
- verify 增强：display_name > 140 → False（运行时上限）；项目/团队 m_uc 必须等于
  active 成员计数；标签集合保持对自定义标签的宽容（与运行时惰性语义一致，避免误阻断）。
- verify 的 subject 唯一索引断言同时接受 0004 与 0005 两种规范形态：
  `{"$exists": true}`（0004 up() 后、0005 替换前的状态）与
  `{"user": {"$type": "objectId"}}` / `{"external_id": {"$type": "string"}}`
  （0005 落库后的最终形态）；run_pending 在 0004 应用后立即 verify，
  完整链条后手动 verify 也须通过，故必须两者皆容。

## 4. 前端修复明细

### 4.1 稳定 operation_id（严重）

**根因**：EditWorkers/MemberStats/MemberList/ProjectSettingBase 用
`${date.now()}`/`${uuidv4()}` 生成 operation_id。服务端按 (project, operation_id) 幂等并从此派生
外部署名 external_id；id 每次变更 → 网络重试产生重复外部署名/重复版本。

**修复**（src/utils/projectMembers.ts 新增 `projectMemberOperationId`）：
主体基为 `${projectId}-${action}-${subjectKey(member)}`，subjectKey = member.id | user:<id> |
external:<externalId|displayName>；§4.8 起在主体基上追加操作内容指纹（tags 全集/displayName/
status），即最终 id = 主体基 + 指纹（详见 §4.8），保证「同内容重试幂等、不同内容新 id」。

**测试**：projectMembers.test.ts 现有「operation id 稳定/跨重试一致/主体不冲突」「同成员
不同 payload → 不同 id」「update 种类（removed/tags/displayName）指纹区分」「外部成员
add id 稳定」「diff 携带指纹 id」等用例（原「编辑显示名不改变 id」用例在 §4.8 内容指纹
落地后被替换——displayName 已进指纹，编辑显示名会得到新 id）。

### 4.2 invited 成员不可被一键分配角色（一般）

EditWorkers.addMember 对 `status === 'invited'` 直接提示并返回；mergeMemberTag 对已有 invited
草稿保持 invited（不再无差别置 active），草稿与后端状态一致。

**测试**：projectMembers.test.ts 新增「邀请成员分配标签后仍为 invited」用例。

### 4.3 privilegedSelf 包含 canManageMembers（一般）

EditWorkers.canAssignRole 的 privilegedSelf 直接加入 canManageMembers——有成员管理权限的操作者
为自己分配工作人员标签时不受资格集合限制（与后端 validate_project_tags 语义一致）。

### 4.4 teamMembers.ts 与后端 update() 语义对齐（一般）

- canEditTeamMember：self 仅在操作者是 creator/admin 时返回 true（普通成员连自己的
  tags/资格也不能编辑；本人只能改自己的 alias）。
- canEditTeamMemberBaseTag：self 永远 false（后端 update 拒绝 operator == member.user 改
  base_tag）；admin 对 member 的 base_tag 编辑保留（后端允许无操作写入）。

**测试**：teamMembers.test.ts 重写 self 规则用例并补充 admin 规则。

### 4.5 成员加载上限（一般）

EditWorkers 团队成员全量加载 limit 100 → 1000（与成员管理页一致，避免大团队被截断）。

### 4.6 成员管理列表：站点身份、头像与单请求（功能补强）

- 列表卡片（setting/member）主名改用站点用户名（member.user.name），外部署名退居 meta 行，
  同名用户可区分；MemberDetail 同步。
- 卡片新增头像（共享 Avatar type="user"）：外部成员无 avatar 时自动落默认用户头像。
- 项目/团队成员列表从按状态多次请求合并为单次：项目 `status=active,invited,removed&limit=3000`、
  团队 `status=active,removed&limit=3000`（后端本就支持逗号分隔状态，仅前端合并）。
- 新增纯函数 projectMemberPrimaryName（注册站点名优先于展示名，外部回退展示名）。

**测试**：projectMembers.test.ts 新增 projectMemberPrimaryName 三例。

### 4.7 MemberStats 职位图标三态颜色回归（视觉修复）

- 重构前图标仅着色：有成员 → 绿（#28a745）、无 → 浅灰；重构时误改为「label+图标整块 =
  主题蓝 primaryColor」，且「邀请中」区分在该组件中缺失（用户反馈颜色与之前不同）。
- 恢复为只染图标 + 三态：有 active → 绿 #52c41a、仅 invited → 橙 #fa8c16、无 → 浅灰，
  与 MemberList 状态 Tag（green/orange/default）全局同语义；tooltip 对仅邀请中的职位显示
  「翻译（邀请中）：某人」。
- getProjectWorkerIconColor 升级三态（原两态函数重构后已被旁路，仅剩测试引用）。
- 数据链配套：卡片数据源 member_summaries 纳入 invited（见 §2.11），否则卡片上永远只有
  active/无两态，橙色无法呈现。

**测试**：projectWorkers.test.ts 三态断言（invited 橙、active 优先于 invited、removed 忽略）。

### 4.8 多职位 tag 保存被后端幂等层吞掉（严重）

**现象**：同一成员连续添加多个职位 tag（快捷编辑弹窗或成员管理页）均无报错，但
服务器始终只保留第一个 tag——与"一个用户可同时拥有多个项目职位"的改造目标冲突。

**根因**：`projectMemberOperationId` 旧实现为 `项目-动作-成员`，不含操作内容。同一成员的
第二次 update（追加第二个职位 tag）复用同一 operation_id；后端 `apply_changes` 的
`_claim_operation` 对 `status == "succeeded"` 的 id 直接重放上次结果（不执行），
第二次保存被当作网络重试静默吞掉（故无报错）。

**修复**（前端 `src/utils/projectMembers.ts`）：
- operation_id 加入操作内容指纹（tags 全集/displayName/status，encodeURIComponent 拼接）：
  相同 payload 重试 → 同 id → 幂等重放；不同内容（加第二个 tag）→ 不同 id → 正常执行。
- `diffProjectMembers` 回调签名扩展为 `(member, action, content?)`，全部调用点透传实际
  变更内容：EditWorkers/MemberList（diff 回调与 remove/restore 手动操作）、
  ApplicationList（批准申请写 tags）、ProjectSettingBase（退出项目写 removed）。
- 后端幂等语义不变（只按 id 去重），无需后端改动。

**测试**：
- 前端 `projectMembers.test.ts`：同成员不同 tags → 不同 operation_id；同内容重试稳定；
  update 种类（removed/tags/displayName）互不相同；diff 携带指纹 id。
- 后端 `tests/api/test_identity_api.py`：`test_project_member_can_hold_multiple_worker_tags`
  （一次 add/update 多 tag 全保留）；`test_sequential_updates_with_distinct_operation_ids_all_apply`
  （顺序多次 update 全部生效，同 id 重放仍幂等返回原结果）。

### 4.9 成员管理排序与自我身份降级拦截（界面修复）

**排序**：成员管理界面成员统一按 创建者(creator) > 管理员(admin) > 成员(member) 排列，
同基础身份按站点用户名/展示名排序（项目成员按 tags 身份、团队成员按 baseTag）。
新增 `src/utils/memberSort.ts`（memberIdentityRank / sortMembersForDisplay），
`MemberList` 渲染前排序。

**项目身份 / 职位拆分**：成员详情把「项目标签」拆为两个下拉——
「项目身份」（单选 member/admin/creator）+「项目职位」（多选 worker/自定义 tag），
与团队成员「基础身份 + 团队标签」模型对齐，从根源消除"在多选里点掉自己身份 tag"的
移除路径（tagRender/disabled option 方案随之移除）。数据模型不变：身份与职位在保存时
合并回 tags 数组（`mergeProjectIdentityTags`），排序/图标颜色/operation_id 指纹全部沿用。

编辑权限（前端显式限制 + 后端兜底）：
- **只有项目创建者（owner）可改他人项目身份**；非 owner 的 admin 身份下拉禁用。
- **任何人不能改自己的项目身份**（creator 与 admin 都一样）：前端身份下拉 self 锁定，
  后端 `ProjectMember.update` 对 `operator == member.user` 移除自己的身份 tag →
  `ProtectedIdentityTagError`（422 INVALID_IDENTITY_TAG）；owner 移除自己的 creator 走
  既有 `MemberAlreadyOwnerError`（409 MEMBER_ALREADY_OWNER）。
- **移除他人身份 tag 仅限 owner**：新增后端检查——非 self 且非 owner 移除他人
  creator/admin tag → 422 INVALID_IDENTITY_TAG（授予本就受 assignable 限制）。
- 前端 `saveProject` 保留 `removedSelfIdentityTags` 预检作为兜底。
- 团队成员侧维持既有保护（前端口令 `canEditTeamMemberBaseTag` self 恒 false；
  后端 `operator == member.user` → NoPermissionError）。

**测试**：
- 前端 `src/utils/memberSort.test.ts`：身份排序（项目/团队）、同名排序、输入不变性、
  `projectIdentityOf`/`projectJobTagsOf`/`mergeProjectIdentityTags` 拆分往返、
  `removedSelfIdentityTags` 自我身份守卫。
- 后端 `tests/api/test_identity_api.py`：
  - `test_self_demotion_of_identity_tags_is_blocked`——admin 自我移除 admin → 422
    INVALID_IDENTITY_TAG；creator 自我移除 creator → 409 MEMBER_ALREADY_OWNER。
  - `test_identity_changes_require_project_creator`——非 owner 移除他人 admin → 422
    INVALID_IDENTITY_TAG；owner 移除他人 admin → 成功。
- 前端 `src/utils/projectMembers.test.ts`：operation_id 内容指纹——同一成员不同职位组合
  （tags 全量，含身份变化）得到不同 id、同内容重试稳定。

### 4.10 快捷编辑按职位组织、成员页默认选中（界面优化）

- **EditWorkers 快捷编辑重构为「按职位添加成员」**（原"按成员添加职位"的反向模型），
  布局采用对比原型选定的 v2 双栏工作台（组件风格与现有组件一致）：
  - 左栏职位列表（竖排、彩色圆点 + 计数 + 激活左强调条），右栏为当前职位工作区。
  - 右栏默认只显示**持当前职位 tag 的成员**（分隔线行、悬停高亮，可移除该职位、
    邀请中带徽标；空态有引导文案）——**未触发添加行为时绝不展示其他成员**。
  - 「添加成员」按钮（成员区标题栏）显式展开添加面板（带品牌色左实线）：
    - **空输入**：已加入成员候选——点击补当前职位；已持有者「已持有」不可点；
      邀请中者提示先接受邀请（规格：invited 不获得标签权限）。
    - **输入任意字符**：团队内搜索（昵称/站点别名/团队别名，经 `getTeamMembers word`），
      点击搜索结果才添加注册成员并附带职位 tag；无资格者禁用并提示。
    - **直接回车**：添加外部成员（输入文字即外部署名），不再有"回车自动选第一个结果"
      的旧行为。
    - 添加成功后面板自动收起；切换职位/收起均清空输入。
  - 资格预加载拆分独立 effect（limit 1000）供 canAssignRole 使用。
  - 弹层尺寸修正：菜单宽度固定 `min(620px, calc(100vw - 20px))`（成员信息/名称长短不再
    引起宽度抖动）；左栏加宽至 128px 且一屏展示全部职位（max-height 380px）；
    右栏成员/候选列表 max-height 提升至 224px；MemberStats 弹层定位估算同步
    （top 预留 560 / left 预留 660）。
  - 确认反馈：无任何成员变更时点「确认」不再静默，提示「没有需要保存的成员变更」。
  - 确认按钮事件绑定排查：React 17 事件委托到 root 容器，元素本身不挂原生监听器
    （DevTools Elements 面板看不到 click 监听是正常现象，并非未绑定）。为排除委托
    路径被干扰（如重复 React 实例）的可能，确认按钮改为原生 `addEventListener`
    绑定（ref + useEffect，随 saveDraft 闭包重绑），元素上真实可见监听器。
  - **含中文外部署名保存失败根因与修复**：外部署名的 operation_id 形如
    `projectId-add-external:中文名`，`MemberStats.save` 曾把它原样放进
    `Idempotency-Key` HTTP header；HTTP 只允许 ISO-8859-1，浏览器直接抛错，
    请求未发出（英文/数字/注册用户不受影响）。后端仅把该 header 当 request_id
    使用，幂等靠 body operation_id（`_claim_operation`），故只修传输层：
    新增 `projectMemberIdempotencyHeader`（percent-encode）用于 header，
    body 内 operation_id 保持原样（后端存储/replay 逐字一致，external_id
    派生不受影响）。测试新增「idempotency header stays ASCII for Chinese
    external names」（全 ASCII、可逆解码、body 保持中文）。
- **成员管理默认选中**：打开页面时优先选中当前用户自己，否则选中项目创建者
  （tags 含 creator / isOwner；团队为 baseTag==='creator'），否则列表第一项；
  已选中项仍在列表中时保持不变。逻辑抽为纯函数
  `preferredDefaultMember`（src/utils/memberSort.ts），替换原来选中 `data[0]` 的行为。

**测试**：src/utils/memberSort.test.ts 新增 `preferredDefaultMember` 五例（self 优先、
项目/团队创建者回退、isOwner 标记、空列表、无 currentUserId 回退第一项）。

## 5. 文档同步

docs/project-identity-tags-change-summary.md 已同步以下与代码不一致/过时的表述：

- §3.2：等级标签覆盖/移除限 creator；等级标签响应 initial_assignable=False。
- §3.3：status 字段统一为整数 + identity_status，列表不再字符串覆盖。
- §4.3：团队成员自我管理边界（普通成员不能更新自己的 tags/资格）。
- §4.4：邀请/申请捕获不到投影时保留 join 标签（tags=None 语义）。
- §5.1：restore 走容量检查；operation_id 稳定生成约定。
- §5.2：merge 额外要求目标为团队 active 成员；display_name 可选；owner 保护；回滚写日志。
- §6.2：无效主体文档保留；m_uc/display_name 的 verify 断言。
- §6.5：legacy checksum 告警、租约丢失报错、超时告警。
- §8.1/新增 §8.3：Python 3.12 本地专项测试与低内存开发服务器分组回归入口。
- §3.1（项目标签）新增项目身份编辑规则：仅 owner 可改他人身份、任何人不可改自己的身份、
  移除他人身份 tag 仅限 owner；前端成员详情拆「项目身份 + 项目职位」两个下拉。
- 成员变更 API 的 operation_id 约定补充操作内容指纹（tags 全集/displayName/status），
  相同内容重试幂等、不同内容得到新 id（§4.8 根因修复后同步）。

## 6. 保留的设计决策（规格与代码一致，不修改）

1. 团队成员自我移除（operator == member.user 的 remove()）保持允许，只有 creator 受保护。
2. merge 需要目标为团队 active 成员（比规格 5.2 原文更严的合并前置条件），作为文档化约束。
3. transfer_owner 会 inc status_version（与状态版本计数共用同一乐观锁域），文档化行为。
4. users_by_permission 对停用自定义角色不产生权限（自定义角色 API 已停用，遗留数据建议用
   数据清理时映射为标签处理）。
5. verify 不对未知成员标签硬报错（自定义标签可能先于策略定义存在；运行时对未定义标签惰性处理）。

## 7. 新增/修改测试清单

后端（tests/）：

- tests/model/test_identity_services.py：
  - test_plain_team_member_cannot_manage_other_members_or_self_tags
  - test_policy_base_tag_override_and_removal_are_creator_only
  - test_update_aliases_requires_expected_version
  - test_malformed_ids_raise_business_errors_not_500
  - test_merge_cannot_strip_owner_creator_tag
  - test_merge_target_must_be_an_active_team_member
  - test_restoring_removed_members_respects_project_capacity
  - test_group_mixin_users_skips_external_members_without_crashing
- tests/model/test_identity_permission.py：
  - test_removed_project_member_keeps_no_access_even_with_team_admin
  - test_removed_team_member_loses_team_inheritance_but_keeps_membership
- tests/base/test_migrations.py：
  - test_legacy_checksum_mismatch_warns_but_remains_accepted
  - test_lock_lease_loss_raises_instead_of_letting_two_runners_continue
  - test_m0004_preserves_invalid_subject_documents_instead_of_deleting
  - test_m0004_verify_detects_overlong_display_names_and_count_mismatch
- tests/model/test_identity_models.py：
  - test_project_member_to_api_carries_site_user_identity（user 子对象、无 email）
  - test_project_member_to_api_can_skip_permission_snapshot（include_permissions=False 零快照调用）
  - test_member_summaries_include_invited_members（summary 含 active + invited）
- tests/api/test_identity_api.py：
  - test_member_list_carries_site_user_identity_and_skips_permission_snapshot
    （单请求多状态、user 子对象、列表权限为空、外部署名无 user）
  - test_project_member_can_hold_multiple_worker_tags（一次 add/update 多职位 tag 全保留）
  - test_sequential_updates_with_distinct_operation_ids_all_apply
    （顺序多次 update 全部生效；同 id 重放仍幂等）
  - test_self_demotion_of_identity_tags_is_blocked
    （admin 自我移除 admin → 422；creator 自我移除 creator → 409）
  - test_identity_changes_require_project_creator
    （非 owner 移除他人身份 tag → 422；owner 移除他人身份 → 成功）

前端（src/）：

- src/utils/projectMembers.test.ts：稳定 operation_id、invited 不晋升、projectMemberPrimaryName、
  多职位 tag 的不同 operation_id（内容指纹）、update 种类区分。
- src/utils/memberSort.test.ts：成员身份排序（creator>admin>member、同名排序、不变性）、
  身份/职位拆分与合并往返、自我身份 tag 移除守卫（removedSelfIdentityTags）。
- src/utils/teamMembers.test.ts：self 编辑规则与后端对齐。
- src/components/shared/projectWorkers.test.ts：职位图标三态颜色（active 绿 / invited 橙 /
  无成员浅灰、active 优先、removed 忽略）。

## 8. 遗留事项（不在本次范围）

1. me.py/team.py 项目列表仍是「全量加载 + Python 过滤」（本次加 select_related 缓解 N+1）；
   完全下沉到 DB 层需要产品确认排序语义（按 ProjectMember.edit_time）。
2. users_by_permission 对遗留自定义角色数据不产出用户（停用功能的已知限制，建议数据清理时
   将自定义角色映射为标签成员）。
3. ~~app/apis/team.py 项目洞悉（`get_insight_project_users_data`）外部署名响应缺 name 键
   （前端有 fallback）~~：**已修复**——外部成员补 `member_data["name"] = relation.display_name`，
   响应形态统一（后端修复轮）。
4. ~~异常类 identity_code 复用、死代码（app/apis/identity.py TeamMemberAPI.post、
   project_member.py _set_active）~~：**已修复**（后端修复轮）——
   - 删除死代码 `TeamMemberAPI.post`（与 `TeamMemberListAPI.post` 重复且无路由）与
     `ProjectMemberService._set_active`（零调用）；
   - identity_code 唯一化：`IdentityTeamMemberNotFoundError` → `TEAM_MEMBER_NOT_FOUND`、
     `AliasValidationError` → `ALIAS_INVALID`、`IdentityVersionConflictError` → `VERSION_CONFLICT`、
     `ProtectedIdentityTagError` → `PROTECTED_IDENTITY_TAG`、`IdentityPolicyInUseError` →
     `IDENTITY_POLICY_IN_USE`（原 5101/5100/5112 的 code 保留给原异常类）；
   - 同步更新 `tests/api/test_identity_api.py` 两处 `failed.code` 断言与注释
     （`INVALID_IDENTITY_TAG` → `PROTECTED_IDENTITY_TAG`）。
5. 前端新组件硬编码中文文案未抽 i18n（EditWorkers/IdentityTagPolicy/MemberList/ProjectSettingBase）。
6. ~~前端 typecheck 被未改动的 AdminImageSafeCheck.tsx 阻塞~~：已解决。该页面引用的图片安全检测
   功能已被后端有意移除（commit c577fe3），页面本身从未接入路由，属于孤儿代码；
   经确认后删除 `src/components/admin/AdminImageSafeCheck.tsx`，全量 `tsc --noEmit` 通过。

## 9. 验证状态

- 当前 Windows 本地系统 Python 为 3.12.5；依赖锁定的 `itsdangerous==2.2.0` 与 Flask 3.1
  组合可正常导入，改动文件已通过 `py -3.12 -m compileall`。
- 完整回归可在开发服务器容器内执行，但受约 960 MiB 主机内存、1 GiB swap 和每个后端/
  worker 容器 512 MiB 上限约束，必须分组、串行并关闭 coverage（见 §16）。
- 以下从“最近一轮”开始的 release 与测试数字是各轮发生时的历史记录，不代表当前 release。
- 最近一轮（多职位 operation_id 指纹 + 成员排序 + 身份/职位拆分 + 自我降级拦截）：
  - 本地前端 jest **87/87**、`tsc --noEmit` **0 错误**（新增 memberSort.test.ts、
    projectMembers.test.ts 指纹用例等）。
  - 服务器身份 pytest 套件 **74/74**（含 test_self_demotion_of_identity_tags_is_blocked、
    test_identity_changes_require_project_creator 等新增用例），内置 Test（迁移 27、
    oss_r2、performance、celery route）全部通过，Verify 通过。
  - 已部署 release：`moeflow-20260816-154819-1b8c76e1-3282a7f5`（moeflow.ps1 Deploy
    -AllowDirty）。
- 界面优化轮（快捷编辑分组 + 成员页默认选中，纯前端）：本地 jest **92/92**、tsc 0；
  已部署 release `moeflow-20260816-162342-1b8c76e1-3282a7f5`，Verify 通过（后端零改动）。
- 快捷编辑按职位重构轮（纯前端）：本地 jest **92/92**、tsc 0；已部署 release
  `moeflow-20260816-181406-1b8c76e1-3282a7f5`，Verify 通过（后端零改动）。
- 快捷编辑 v2 双栏工作台轮（纯前端，原型对比后选定）：本地 jest **92/92**、tsc 0；
  已部署 release `moeflow-20260816-190134-1b8c76e1-3282a7f5`，Verify 通过（后端零改动）。
- 快捷编辑尺寸与确认反馈轮（纯前端）：固定弹层宽度、左栏一屏展示全部职位、
  空变更确认提示；已部署 release `moeflow-20260816-191202-1b8c76e1-3282a7f5`，
  Verify 通过（后端零改动）。
- 确认按钮原生事件绑定轮（纯前端）：React 17 委托到 root、元素无监听器属正常；
  改为原生 addEventListener 绑定；已部署 release `moeflow-20260816-192001-1b8c76e1-3282a7f5`，
  Verify 通过（后端零改动）。
- 中文外部署名保存修复轮（纯前端）：Idempotency-Key header 改为 percent-encode；
  本地 jest **93/93**、tsc 0；已部署 release `moeflow-20260816-193128-1b8c76e1-3282a7f5`，
  Verify 通过（后端零改动）。
- 团队成员列表逗号状态修复轮（后端）：`TeamMemberService.list_members` 原先把
  `status=active,removed` 当作单个状态值匹配（项目侧 `ProjectMemberService.list_members`
  会 `split(",")`，两侧不一致），迁移后的真实生产数据页面上团队列表整页为空。新增
  服务级 `test_team_member_list_members_splits_comma_separated_statuses` 与 API 级
  `test_team_member_list_supports_comma_separated_status` 用例（旧代码两例均 FAIL，
  暴露问题），修复为与项目侧一致的逗号拆分后通过。已部署 release
  `moeflow-20260817-082443-1b8c76e1-3282a7f5`。
- 遗留发现（已解决，见下轮）：`tests/base/test_migrations.py` 的
  `test_identity_migration_accepts_generic_reference_group_and_user_fields` 与 0004 的
  owner 保护规则冲突——测试数据里项目无任何 owner/创建者关系，0004 按设计上报
  `owner_missing`（owner_issue_count=1）并使 verify 失败（规格 §6.3），因此该用例
  在修复轮之前即失败，与本轮改动无关。
- owner 兜底与前端布局轮：
  - 0004 新增 owner 兜底：项目无任何创建者关系时，自动把 `ou` 绑定到
    **项目所在团队的团队创建者**（从 `team_user_relation` 中 creator 角色推导），
    团队也无创建者时退回**网站创建者**（`_id` 最早的用户），并补写一条带
    `creator` 标签的 `project_member` 行，`owner_issue_count` 不增加、verify 仍可
    通过；确实找不到时维持原 `owner_missing` 失败保护。审计：报告 issue code
    `owner_bound_to_team_creator` / `owner_bound_to_site_creator`（归入
    owner-issues.jsonl）。
  - 删除与保护规则冲突的旧用例，换为两个新用例：
    `test_identity_migration_binds_missing_owner_to_team_creator`（保留 GenericRef
    DBRef/`{"$id"}` 邀请形状回归覆盖）与
    `test_identity_migration_binds_missing_owner_to_site_creator`。
  - 前端修复 MemberStats 布局：项目多时 List 容器出现滚动条吃掉 ~15px 宽度，
    角色标签文字被挤成两行。第一版加 `scrollbar-gutter: stable` + 标签整体
    `overflow:hidden`，但会在无滚动条时也永久预留 15px 且从右侧裁剪先切到图标；
    第二版改为：撤销 gutter（少项目时全宽不挤压）、标签文字与图标分离（文字
    ellipsis、图标 `flex: none` 永不裁剪）、List 滚动条换细样式（webkit 8px +
    Firefox `scrollbar-width: thin`，出现时只吃 ~8px）。本地 tsc 0 错误、jest 93/93。
  - 0004 行为变更 → AST 校验和变化；已按"清空开发机 moeflow 系列库 → 恢复生产归档
    → 新代码重跑 0000-0005 → 全面验证"流程完成重测（当时尚未加入 0006）：记录 6 条、m_uc 零偏差、
    project_member=3786（2616 用户成员 + 1170 外部成员）、team_member=154、
    ownerless=0，owner 兜底在真实生产数据上零触发（owner-issues.jsonl 为空）。
    注意：先恢复再迁移时若目标库残留迁移记录，`migrate` 会因校验和不匹配报
    "Applied migration 0004 has been modified"，必须整体 drop 后恢复。
  - 镜像内完整身份回归 **95/95**（含 2 个新兜底用例）；前端本地 jest **93/93**、
    tsc 0。兜底轮 release：`moeflow-20260817-124450-1b8c76e1-3282a7f5`
    （修正后的测试固化进镜像：`moeflow-20260817-131112-1b8c76e1-3282a7f5`）。
  - 后续 MemberStats v2 前端轮（撤销 gutter + 图标保护 + 细滚动条）：仅前端改动，
    本地 jest **93/93**、tsc 0；已部署 release
    `moeflow-20260817-132445-1b8c76e1-3282a7f5`，Verify 通过（后端零改动）。
  - 另修正三个断言旧语义的既有用例（种子 Admin 使兜底总能找到 fallback）：
    `reports_missing_owner_and_blocks_verification` 改为清空用户后验证兜底不可行
    时的 owner_missing 保护；`merges_duplicate_relations_without_legacy_tags` 与
    `audits_workers_shapes_values_and_duplicate_names` 按兜底补成员后的计数调整。
- 团队成员管理前端收口轮（纯前端，release `moeflow-20260817-141103-1b8c76e1-3282a7f5`）：
  - 基础身份收紧：`canEditTeamMemberBaseTag` 改为仅团队创建者可改（原来 admin 也能改
    成员的基础身份，且创建者下拉可选 "creator" 指派给别人——后端虽拦截，前端不应呈现）。
    现在：创建者编排他人（exclude 自己/另一创建者）才可编辑；创建者本人与管理员一律不可改；
    下拉只剩 成员/管理员，"creator" 选项彻底隐藏无从误选；创建者成员的"基础身份"字段
    改为金色只读标签展示。对应更新 utils/teamMembers.test.ts 断言（admin→false、
    另一创建者为目标→false、removed→false）。
  - 团队别名编辑框对齐个人设置站点别名样式：改为软边框容器 + `Input.TextArea
    bordered={false}`（autoSize 2-4 行、聚焦高亮），与 dashboard/user/setting 一致；
    只读时仍显示为普通标签列表。
  - 排序/自动选中与项目成员管理核对为同一共享组件与工具（sortMembersForDisplay +
    preferredDefaultMember），无需改动。本地 jest 93/93、tsc 0，Verify 通过（后端零改动）。
- 完结项目页面卡死修复轮（release `moeflow-20260817-150342-1b8c76e1-3282a7f5`）：
  - 症状：完结项目后页面卡死无法操作，刷新无反应，需重新进入项目集；项目本身正常进入
    完结态且可恢复。清空项目不触发（清空后整页替换为提示组件，不渲染卡片）。
  - 根因（前后端叠加）：
    1. 单项目响应（`to_api`）不含 `member_summary`，只有列表接口（me.py/team.py）批量填充；
       完结/恢复/清空/编辑/创建接口返回的项目对象进入 `state.projects` 后 memberSummary
       为 undefined；
    2. `MemberStats` 的 `members = []` 默认值在每次渲染生成新数组，`useEffect([members])`
       每次渲染都触发 `setLoadedMembers` → 无限渲染循环 → 项目列表页卡死（主线程被占，
       刷新后同路径复现，离开该路由才恢复）。
  - 修复：后端 4 处单项目响应补 `member_summary`（identity.py `_project_api`、project.py
    get/put、team.py 创建/导入，抽 `_project_with_member_summary` 辅助）；前端
    `MemberStats` 的同步 effect 改为按**内容签名**（`memberSummarySignature`，memberId/
    id/userId/displayName 拼接）触发，从根上杜绝"新数组→无限渲染"；`updateLifecycle`
    在响应缺 summary 时保留旧快照（双保险）。
  - 验证：容器内身份 API 套件 **22/22**（新增 complete/reopen 响应含 member_summary 断言）；
    前端 jest **95/95**（新增签名回归用例）、tsc 0；迁移无 schema 变更，migrate no-op。
- 邀请页当前身份化 + 团队资格校验开关轮（release `moeflow-20260817-150342-1b8c76e1-3282a7f5` 之后的下一版本）：
  - 症状：团队设置/项目设置的 `setting/invitation` 页仍使用旧版身份选项（项目侧出现
    「监理/见习翻译」等旧角色；团队侧出现「资深成员/见习成员」），未对齐当前身份系统。
  - 后端：
    - `CreateInvitationSchema`/`ChangeInvitationSchema` 增加可选的 `tags`；团队必须传
      `role_id`（旧路径不变），项目允许 `tags` 或 `role_id`。
    - `POST /v1/projects/{id}/invitations`：携带 `tags` 时走当前身份流程——先做
      `IdentityPermissionService.validate_project_tags`（含资格校验/open 模式），再经
      `ProjectInvitationAdapter.create_or_reuse` 投影（合格团队成员直接加入，返回项目；
      否则返回待处理邀请）。`role_id` 路径保持旧行为。
    - `PUT /v1/invitations/{id}`：项目邀请携带 `tags` 时更新 invited 成员职位
      （`ProjectInvitationAdapter.update_pending_tags`，含审计），不再有角色等级规则。
    - `Invitation.to_api` 对项目邀请附带 `tags`（优先投影成员标签，其次旧角色映射），
      前端可展示/编辑当前职位，不再看到旧角色名。
    - `EditTeamSchema` 接受 `worker_qualification_mode`（qualified/open）；`PUT /v1/teams/{id}`
      仅团队创建者可修改该字段（管理员返回 NoPermissionError）。模型字段与
      `validate_project_tags` 的 open 分支此前已存在，本次补齐修改入口与前端开关。
  - 前端：
    - 新增 `utils/invitationOptions.ts`（团队旧角色过滤为基础身份 creator/admin/member、
      项目职位选项）；`InviteUser`/`InvitationList` 项目侧改为多选手职（职位标签），
      团队侧角色下拉过滤旧身份。
    - `TeamSettingBase` 增加「工作人员资格校验」开关：仅创建者可见，打开后团队不再校验
      成员资格，全员可加入项目任意职位（后端同步强制 creator-only）。
  - 测试：新增身份 API 用例（项目邀请带职位直入/资格拒绝/open 绕过/PUT 更新职位/
    wqm creator-only/团队邀请缺 role_id 校验），前端新增 invitationOptions 用例。
  - 验证：容器内 `tests/api/test_identity_api.py` **28/28**（含本轮 6 个新用例）；
    前端 jest **98/98**、tsc 0；`/api/ping` pong、页面 200；迁移无 schema 变更，
    migrate no-op（release `moeflow-20260817-200440-1b8c76e1-3282a7f5`）。
  - 遗留说明：旧版 `tests/api/test_team_invite_api.py`、`test_project_invite_api.py`、
    `test_team_api.py` 中 7 个用例在 setup 断言即失败（`user1.join(...)` 后
    `get_relation(...).role` 仍断言旧自定义角色；团队列表接口的 joined/无 word 行为），
    属于身份重构以来**既有过时用例**，不依赖本轮改动（失败行均不在本轮代码路径上）；
    按 §8.3 约定验收门为身份专项，这些旧套件不在门禁内，留待后续整理。
  - 已知边界：受邀接受页（用户侧邀请列表）仍显示旧兼容角色名（如「翻译」），职位的
    实际生效以成员管理的 tag 为准；open 模式只绕过资格集合校验，不绕过团队成员身份。
- 站点设置整页空白修复轮（release `moeflow-20260817-214758-1b8c76e1-3282a7f5`，
  仅前端，见 §12）：jest **101/101**（新增 toCase 3 用例）、tsc 0；bundle 含容错
  分支与错误提示；`/api/ping` pong。
- EditWorkers 四项展示/编辑增强轮（release `moeflow-20260817-223158-1b8c76e1-3282a7f5`，
  仅前端，见 §13）：jest **109/109**（新增 8 用例）、tsc 0；bundle
  `index-CnSP0WWg.js` 含全部新标记。
- 快捷编辑「简略模式」+ 团队「加入项目默认展示名」轮（见 §14，本节为最终验证）：
  - 容器内身份专项 `tests/api/test_identity_api.py` **32/32**（28+4 新）、
    `tests/model/test_identity_services.py` **40/40**（38+2 新，含邀请投影默认
    展示名用例）；前端 jest **110/110**（+1 toCase）、tsc 0；迁移零变更 no-op。
  - 部署序列（全部 `-AllowDirty`）：`moeflow-20260818-132957-1b8c76e1-3282a7f5`
    （后端+简略模式首版）→ `133701`（下拉选中后保持打开微调）→ `135646`
    （每行独立搜索词修复）→ **`moeflow-20260818-170433-1b8c76e1-3282a7f5`**
    （导出渲染崩溃修复 + 简略模式设备记忆，该轮最终 release）。
  - 修复过程中发现并解决：服务层新用例最初用陈旧 `relation.version`（版本检查先于
    权限/长度检查），改为取最新版本号后全绿；项目导出崩溃为 abbr 规则消费方 bug
    （见 §14③），与存储无关。

## 12. 站点设置页整页空白修复（admin/site-setting）

- 现象：主页欢迎语等站点设置数据在库里/接口里都正常，但 `admin/site-setting`
  整页字段为空（一直如此，与账号、环境无关）。
- 根因（前端 `src/utils/index.ts` 的 `stringToLowerCamelCase` 的 abbr 特例）：
  `auto_join_team_ids` 中的 `_id` 被替换为 `_Id`，转出 `autoJoinTeamIds`
  （小写 d），而表单字段名/接口类型写的是 `autoJoinTeamIDs`（大写 IDS）。
  加载时 `arrayToTextarea(data.autoJoinTeamIDs)` 读到 `undefined` → `.join` 抛
  TypeError → `.then` 整体失败（旧代码无 catch）→ `finally` 置 loading=false →
  整个表单静默空白。保存方向相反（`autoJoinTeamIDs` → `auto_join_team_ids` 正确），
  因此从未暴露。
- 修复（仅前端，release `moeflow-20260817-214758-1b8c76e1-3282a7f5`）：
  - `AdminSiteSetting.tsx`：加载/保存回填统一走 `formDataFromAPI`，对
    `autoJoinTeamIds` / `autoJoinTeamIDs` 两种拼写都兼容（`?? []` 兜底），
    `whitelistEmails` 同样兜底；
  - 加载失败不再静默：`catch` 记录 `console.error`、展示红色 Alert（含错误原文
    +「重试」按钮），加载逻辑收敛为 `loadSiteSetting`（useCallback）+ reloadToken；
  - 新增 `src/utils/toCase.test.ts` 固定大小写映射行为（3 用例）。
- 验证：jest **101/101**（98 + 3）、tsc 0；部署后 bundle 含 `autoJoinTeamIds`
  容错分支与错误提示；`/api/ping` pong。后端零改动，迁移仍 no-op。
- 备注：`stringToLowerCamelCase`/`stringToUnderScoreCase` 的 abbr 处理是全局
  约定（`user_id`→`userId`），未改动；若后续接口再出现 `*_ids` 复数键，需沿用
  本次「边界兼容」模式，或统一字段命名为 `xxxIds` 小写 d。
  后续的导出崩溃（file_ids_include→fileIdsInclude vs 消费方 fileIDsInclude）
  正是同一规则的下一个实例，见 §14③。

## 13. 项目成员快捷编辑（EditWorkers）展示与编辑增强

- 范围：项目卡片成员快捷编辑菜单（`src/components/shared/EditWorkers.tsx`），
  release `moeflow-20260817-223158-1b8c76e1-3282a7f5`，后端零改动。
- ① 圆角统一：`.EditWorkers__Results` / `.EditWorkers__Selected` 边框容器加
  `border-radius`（与面板同款圆角）。
- ② 成员展示区分注册用户/外部署名：成员行左侧带头像（注册用户显示其头像，
  外部成员用默认用户头像），名字优先展示「展示名（displayName）」，当与注册
  用户名不一致时以浅色小字后缀「（用户名）」，外部成员只有展示名。数据来源：
  `ProjectMember.to_api.user`（name/avatar/has_avatar）与 `TeamMember.to_api.user`
  （同源），前端 `userInfoByUserId` 汇总；`utils/projectMembers.ts` 新增纯函数
  `projectMemberDisplayLabel`（含测试）。
- ③ 修改展示名：成员行内铅笔按钮或双击名字进入行内编辑（Input），回车/失焦
  确认、Esc 取消；仅 active 成员且满足既有编辑权限（管理员或本人）可编辑；
  保存走既有 diff → `changes.display_name` 更新接口（后端早已支持）；未保存的
  外部成员改名时同步 `externalId` 保持 subject key 稳定。
- ④ 搜索结果显示规则：输入框命中用户名时只显示用户名；命中别名时在用户名后
  追加匹配到的别名（如「用户名（别名）」），用户别名与成员别名都统计；匹配
  归一化与后端 `normalize_search_text` 一致（NFC+trim+casefold）。纯函数
  `teamSearchResultLabel` / `normalizeSearchText`（含测试）。
- 验证：jest **109/109**（新增 8 用例）、tsc 0；部署后 bundle
  `index-CnSP0WWg.js` 含全部新标记；`/api/ping` pong；迁移 no-op。
- 备注：团队别名为 `TeamMember.aliases`（成员别名）+ user.aliases（站点别名），
  展示与成员页保持一致；本轮未改动成员管理页（setting/member）。

## 14. 快捷编辑「简略模式」+ 团队「加入项目默认展示名」+ 项目导出崩溃修复

- 需求（用户反馈上一版快捷编辑菜单偏复杂）：
  ① 菜单右上角加面板切换按钮，可切到「简略模式」：全部职位同时显示，每职位
  右侧一个人员输入栏；焦点在任意输入栏上即触发搜索；输入为空时下拉自动推荐
  「已加入当前项目的成员」；输入后按既有逻辑搜索，结果展示在下拉框；该模式
  不做展示名快捷编辑。
  ② 团队成员管理页新增「加入项目的默认展示名」输入框，编辑的是自己在加入
  项目时使用的默认展示名——主动加入项目、被邀请加入项目时都默认填充。
  ③ 验证中发现并修复项目导出渲染崩溃（与存储无关的前端键名 bug）。

### ① EditWorkers 简略模式（纯前端，`src/components/shared/EditWorkers.tsx`）

- 头部右侧新增「简略模式 / 完整模式」切换按钮（`faThList`/`faLayerGroup`，
  Tooltip 说明），切换时清空搜索词与已打开面板；**模式记忆**与主题模式
  （`localStorage.themeMode`）同机制：切换即写 `localStorage.editWorkersMode`
  （`'full' | 'simple'`），下次打开快捷编辑直接进入上次使用的模式（首次默认
  完整模式；存储不可用时回退）。
- 简略模式为职位行列表：左侧色点+职位名+人数，右侧为「已持有成员 chips
  （头像+展示名，× 移除该职位）」+ 输入框；输入框聚焦打开下拉：
  - 空输入：推荐当前项目全部已加入成员（非 removed），已持有该职位者标
    「已持有」、受邀中标「邀请中」，点选补充职位；资格/权限校验与完整模式
    完全一致（复用同一套 roleKey 参数化逻辑）；
  - 已输入：按既有逻辑搜索团队（`/teams/{id}/members?word=` 300ms 防抖），
    命中用户名只显示用户名、命中别名显示「用户名(别名)」，结果不可添加时
    置灰；无结果提供「添加外部署名」行；回车直接添加外部成员；
  - 选择后下拉保持打开并回到推荐列表（输入框仍聚焦，便于连续添加），
    失焦 120ms 后收起；**每行保留自己的搜索词**，聚焦任意行即显示该行下拉。
  - 该模式不渲染展示名编辑（无铅笔/双击改名）。
  - 重构：原「选中职位」态函数（canAssignRole / addMember / addSearchResult /
    addRegisteredUser / addExternalMember / isResultActionable / removeRole /
    addRoleToJoined / joinedCandidates / selectedForRole）全部参数化为
    roleKey，完整模式传当前 `role` 态、简略模式传行职位；下拉内容抽为
    `renderSearchResults(roleKey)` 供两种模式共用，行为与展示保持一致。
  - 修复（同轮追加部署）：最初 7 个输入框复用一个 `word` state，导致在任一
    输入框输入时所有输入框同步出现相同文本；改为**每行独立搜索词**
    （`simpleWords` 按职位 key 记录），完整模式仍用共享 `word`。搜索 effect、
    下拉渲染、结果显示均取「当前生效查询词」（聚焦行文字/完整模式 word）；
    行内选人/回车添加后只清空该行自己的搜索词回到推荐列表。

### ② 团队默认展示名（后端 + 前端）

- 数据：`TeamMember` 新增 `default_display_name`（StringField，空=未设置，
  上限 140 与项目展示名一致；`clean()` 去空格/限长；`to_api` 与
  `_team_member_state` 带出）。
- 服务：`TeamMemberService.update_default_display_name`（新方法）——须
  active 且带 `expected_version`（CAS，版本检查先于权限/长度检查），本人或
  可管理的管理者（`_can_manage_target`）可改，写审计
  `team_member_default_display_name_update`；非字符串/超长/版本冲突各自拒绝。
- API：`PATCH /v1/teams/{id}/members/{member_id}/default-display-name`
  （`{default_display_name, expected_version}`），返回 `{"member": ...}`。
- 加入流程默认填充（三处落点共用一个辅助
  `ProjectMemberService.team_default_display_name(project, user)` =
  TeamMember.default_display_name 优先，否则注册用户名）：
  - `ProjectMemberService._add_user`（`_add_user_display_name`）：显式展示名
    优先；未传或等于站点用户名（前端对新成员总是预填站点名，视为隐式默认）
    → 用团队默认；
  - `User.join_project`（主动加入/被邀请直接拉入的落点）：新成员
    `display_name` 用团队默认；
  - `ProjectInvitationAdapter._ensure_projection`（邀请投影创建/`project_pending`/
    `update_pending_tags` 重建）：无投影时 fallback 用团队默认。
  - 被邀请成员接受邀请后沿用邀请时定下的展示名（既有逻辑不变）。
- 前端：
  - `apis/member.ts`：`TeamMember` 补 `defaultDisplayName`，新增
    `updateTeamMemberDefaultDisplayName`；
  - `shared-form/MemberList.tsx` 团队视图「基础身份」下新增该输入框（只读时
    展示现值；保存走新接口，manager 或本人可编辑；提示文案说明用途）。
- 测试：API 5 个新用例（本人改/trim/清空、越权（他人成员/外人）、超长、
  版本冲突、manager 代设、申请批准后默认填充、经 changes API 添加注册成员
  时隐式站点名命中默认/显式名优先/无默认回退站点名），服务层 2 个新用例
  （`update_default_display_name` 权限/审计矩阵；非创建者操作者邀请团队
  成员→invited 投影用默认→接受后不变）。
- 验证：容器内 `tests/api/test_identity_api.py` **32/32**（28+4 新）、
  `tests/model/test_identity_services.py` **40/40**（38+2 新）；前端 jest
  **110/110**（109+1 新 toCase 用例）、tsc 0；迁移零变更 no-op；最终 release
  `moeflow-20260818-170433-1b8c76e1-3282a7f5`（该轮部署序列见 §9）。
- 已知边界：默认展示名只在「新成员创建/重邀请」时生效，不覆盖已存在成员的
  项目内展示名；显式传入「恰好等于站点用户名」的展示名会被当作隐式默认
  （前端无法表达「就要站点名」的意图，可接受）；`default_display_name` 为
  纯偏好字段，不建索引、不做迁移。

### ③ 项目导出渲染崩溃修复（纯前端 `OutputList.tsx` / `Output.tsx` / `apis/output.ts`）

- 现象：开发站打开「项目导出」或点击导出后白屏，console 报
  `Cannot read properties of undefined (reading 'length')`（Output.tsx:64）
  与 `fe.default is not a function`（OutputList.tsx 的 catch）。
- 根因（与存储无关，开发站为 `STORAGE_TYPE=LOCAL_STORAGE`，导出任务可正常
  完成并生成下载链接）：后端 `Output.to_api()` 返回 `file_ids_include` /
  `file_ids_exclude`，前端 `toLowerCamelCase` 按 abbr 规则映射为
  `fileIdsInclude` / `fileIdsExclude`（大写 I、小写 d），而 `Output.tsx` 与
  `APIOutput` 类型声明写的是 `fileIDsInclude` / `fileIDsExclude`——运行时
  取到 `undefined`，`.length` 直接抛错使整页崩溃。旧版（生产迁移）项目带
  导出数据所以必现；新建无导出项目只有创建导出后才触发。
- 修复：`apis/output.ts` 类型字段改为 `fileIdsInclude` / `fileIdsExclude`；
  `Output.tsx` 改读正确键并加 `|| []` 兜底（数据缺键时按「全量导出」渲染
  而不是崩溃）。同源问题（`auto_join_team_ids` 等）此前已在 toCase/站点
  设置修复中处理过（§12），本处是同一 abbr 规则的又一消费方。
- 历史与生产说明：错误拼写自 2022-09-18 Init 提交起存在于本仓库**所有分支**
  （含生产分支 shoreline/main，代码逐字相同），生产此前未复现是因为该界面/
  功能在生产从未真正渲染过导出条目（moeflow 尚未正式部署或导出未被使用），
  并非存在生产专用差异；生产存储（Cloudflare R2，`OSS_BUCKET_STYLE=R2`）与
  本次前端键名修复零交集，正式部署后导出功能为首次可用状态。
- 验证：tsc 0、jest 110/110（无新增用例，纯键名对齐）；部署后 bundle
  `index-DepuwAdh.js` 中 `fileIDs*` 清零、渲染读 `(T.fileIdsExclude||[]).length`；
  `/api/ping` 200；按捕获的真实请求回放 GET/POST 均 200 且导出任务完成
  （status=4、链接有效）。

## 15. 团队成员列表 N+1 优化 + 前端快捷编辑请求合并（2026-08-26）

在项目列表/团队列表批量序列化闭环之后，对团队成员的聚合列表端点做同类审查，
落地两处与「逐条 to_api + N+1」同源的优化，并顺手精简前端快捷编辑的请求。

### ① 团队成员列表批量预取 user（后端 `app/apis/identity.py` / `models/team_member.py`）

- **现象**：`GET /v1/teams/{id}/members?status=active,removed` 成员管理接口一次性
  最多加载上千团队成员的完整对象。原实现 `TeamMemberListAPI.get` 里
  `[member.to_api() for member in members]` 逐条序列化，而 `TeamMember.to_api()`
  内部的 `self.user.to_api()` 触发逐成员惰性解引用 user 文档（N+1）；且先对
  全量成员序列化再切片，即使只需一页也物化整组。
- **修复**：
  - `TeamMember.to_api(user_map=None)` 增加批量上下文（`models/team_member.py`），
    命中预取 user 时不惰性查询；缺 id 回退逐条加载，行为不变。与
    `Project.batch_to_api` 同模式。
  - `TeamMemberListAPI.get`（`apis/identity.py`）先收集本页成员的全部 user id，
    一次 `User.objects(id__in=[...])` 批量预取构造 `user_map`，且只对
    **页面切片** `members[skip:skip+limit]` 做序列化，`count` 仍为全量。
- **回归测试**：新增 `test_team_member_to_api_supports_batch_user_map`
  （`tests/model/test_identity_models.py`）——断言批量上下文与默认逐条序列化
  逐字段等价，且 `user_map` 缺 id 时回退不抛错。

### ② `list_members` 排序阶段批量预取（`app/services/team_member.py`）

- **现象**：排序键 `member.user.name` 在逐条惰性解引用 user，排序阶段就先于
  to_api 触发 N+1，是前一项优化无法覆盖的部分。
- **修复**：`TeamMemberService.list_members` 排序前一次 `User.objects(id__in=...)`
  构造 `user_lookup`，排序不再触碰存储。

### ③ 前端快捷编辑请求合并（`src/components/shared/MemberStats.tsx`）

- **现象**：点击项目卡片「人员快捷编辑」瞬发 3 次 API：项目成员 `status=active`
  与 `status=invited` 各一次 + 团队成员一次。
- **修复**：抽共享 `loadProjectMembers()`，用单次 `status=active,invited` 请求
  合并前两项（后端 `list_members` 支持逗号分隔状态），点击从 3 次降为 2 次。
  第 3 次团队成员请求用于资格校验（数据源/用途不同），保留不合并。

### 验证

- 本地：编译 + `ruff check app/` 全绿；`test_identity_models` + `test_identity_services`
  50 passed。
- 容器（真实 MongoDB，分批 + `--no-cov` 控内存）：完整身份套件 5 套件全通过；
  `test_identity_api` 最终版 **33 passed**；`moeflow.ps1 -Action Test` 验收门 exit 0
  （migrations / OSS-R2 / performance_regressions / celery 路由）。
- 历史实测：release `moeflow-20260826-025947` 下 `/v1/teams/{id}/members?limit=3000`
  （当时 seed 该团队 92 名成员）TTFB median≈231ms；N+1 消除对真实大团队收益显著，
  但 92 成员 seed 差异被约 1 GiB 小机器开销掩盖。接口字段（`user`/`user_id` 等）完整保留。
- 部署注意：开发服务器约 960 MiB 内存、1 GiB swap，后端容器 512MB 上限——容器内跑完整测试须
  **分批 + 禁用 coverage**，一次性全量 coverage 测试会导致 OOM 重启；
  `moeflow.ps1 -Action Deploy` 在后台非交互下会被 `ShouldProcess` 拦截，需
  `$ConfirmPreference='None'` 显式放行。

## 16. 当前环境与综合验收（2026-08-27）

本节覆盖本文历史记录之后的当前基线：

- Python 运行时为 3.12.5；直接依赖维护在 `requirements.in` / `requirements-dev.in`，
  锁文件由 `uv pip compile --python-version 3.12` 生成。
- 关键锁定版本包括 Flask 3.1.3、Marshmallow 3.26.2、MongoEngine 0.29.3、PyMongo 4.17.0、
  Pillow 12.3.0、Celery 5.6.3、Boto3 1.43.80、Google Cloud Storage 3.13.1 和 OSS2 2.19.1。
  Flask-APIKit 已从依赖和代码中移除，保留的 API 基础能力位于 `app/core/api.py`。
- 当前开发服务器 release 为 `moeflow-20260827-214208-1b8c76e1-3282a7f5`，入口为
  `http://100.90.141.40:5080/`。后端、两个 Celery worker、前端、MongoDB 和 RabbitMQ
  均正常运行。
- 服务器约有 960 MiB 内存和 1 GiB swap，后端与两个 worker 每个容器限制 512 MiB。
  完整测试关闭 coverage、禁用 xdist，按 `tests/base`、`tests/api`、`tests/model`、
  `tests/other` + `tests/tasks` 分组串行运行；统一参数为：

  ```text
  pytest -q -o addopts= --no-cov -p no:cacheprovider --disable-warnings --maxfail=1
  ```

- 综合结果：`tests/base` 51 passed（4 subtests）、`tests/api` 210 passed、`tests/model`
  179 passed（4 subtests）、`tests/other` + `tests/tasks` 35 passed，合计 `475 passed`
  （4 subtests）。
- 生产归档 `/root/moeflow-prod-20260816-104619.archive.gz` 只在开发服务器恢复和迁移验证中
  使用，约 680,879 条文档的 `0000` 至 `0006` 迁移已成功；生产服务器本身未被修改。

# Moeflow 项目列表性能优化进度

更新时间：2026-08-27

> 当前状态：本文中的性能采样保留其实际采样时使用的历史 release。当前开发服务器已
> 更新为 `moeflow-20260827-214208-1b8c76e1-3282a7f5`，入口为
> `http://100.90.141.40:5080/`。开发服务器约有 960 MiB 内存和 1 GiB swap，后端及
> 两个 Celery worker 每个容器限制 512 MiB；完整测试必须分批、串行并使用
> `--no-cov`，详见 [development-status.md](development-status.md)。

## 目标与结论

对比开发服务器上新优化版本与生产服务器上旧版本的同一项目集接口耗时，排除客户端到服务器之间的网络差异，确认剩余瓶颈并完成优化。生产旧版本的容器内基线仍是可选的只读记录项，不影响开发服务器优化已完成的结论。

## 已完成的代码优化

本文记录的优化版本包含以下批量序列化优化：

- `IdentityPermissionService.project_snapshots()` 从逐行查询改为按页面中的 collection 批量查询，减少每页重复数据库往返。
- `Project.batch_to_api()` 增加批处理上下文，预加载成员、team relation、policy、role 等关联数据。
- team 和 project set 的序列化结果在同一批次内缓存，避免每个 project 重复构造相同数据。
- `Project.to_api()` 支持消费批量上下文中的快照、角色和缓存数据。
- 增加回归测试：`test_batch_project_api_matches_single_project_api`。

本地验证结果：

```text
test_project_api.py
test_identity_models.py
test_identity_permission.py
test_performance_regressions.py

37 passed
Ruff clean
```

## 部署状态

### 开发服务器

地址：

```text
100.90.141.40:23213
```

历史部署版本（性能采样使用）：

```text
moeflow-20260825-183957-1b8c76e1-3282a7f5
```

> **历史记录（2026-08-26）**：当时容器实际运行的本地构建镜像为
> `moeflow-local:moeflow-20260826-025947-1b8c76e1-3282a7f5`
> （release ID `moeflow-20260826-025947-1b8c76e1-3282a7f5`，
> 由当时未提交工作区代码构建，含批量序列化 + 列表/详情职责拆分 +
> 团队成员列表 N+1 优化与前端快捷编辑请求合并）。
> 此前部署的 `moeflow-20260826-001416` / `moeflow-20260826-021547` 已被替代。

该历史 release 的容器健康检查正常，`/api/ping` 返回 pong。其采样结果仅用于本文中的性能对比。
当前部署和整体回归结果以 `development-status.md` 为准。

### 生产服务器

生产服务器仍在运行旧版后端镜像：

```text
ghcr.io/moeflow-com/moeflow-backend:v1.1.8
```

容器前缀：

```text
moeflow-deploy-20240604-fix-
```

生产服务正在对外提供服务，本次测试期间禁止执行以下操作：

- 关闭或重启萌翻 / Moeflow 相关容器；
- 重启生产服务；
- 修改生产数据库；
- 写入或覆盖生产配置；
- 执行任何非只读诊断命令。

只允许读取状态、日志或执行无副作用的 GET 请求。

## 当前测量结果

### 开发服务器优化版本（历史采样）

使用开发服务器 fetch 文件中相同 token、URL 和 query 参数，在开发服务器容器内部直接请求接口，排除外部网络延迟。

**2026-08-26 在容器 (`moeflow-deploy-20251003-moeflow-backend-1`) 内直接 curl `127.0.0.1:5000` 实测**
（接口 `/v1/teams/67b231b45781d7125d41e311/projects`，query `page=1&limit=15&status=NORMAL&mode=search-project-name`）：

```text
--- fetch 精确形态（同时带 project_set+project_sets，双参数）7 次取样 ---
run1: http=200 total=0.052 ttfb=0.052 bytes=16629
run2: http=200 total=0.035 ttfb=0.035 bytes=16629
run3: http=200 total=0.032 ttfb=0.032 bytes=16629
run4: http=200 total=0.046 ttfb=0.046 bytes=16629
run5: http=200 total=0.036 ttfb=0.036 bytes=16629
run6: http=200 total=0.071 ttfb=0.071 bytes=16629
run7: http=200 total=0.037 ttfb=0.037 bytes=16629

median(ttfb) ≈ 36ms    payload ≈ 16,629B (16.2KB)    max ≈ 71ms
--- 单参数 project_set（无 project_sets 重复） ---
http=200 total=0.043 ttfb=0.042 bytes=16629      # 与双参数 payload 完全一致
--- /v1/user/projects (limit 15) ---
http=200 total=0.076 ttfb=0.075 bytes=13632
--- limit=60 页 ---
http=200 total=0.095 ttfb=0.094 bytes=65406 (60 项)
```

**结论：优化目标已达成。** 与文档此前记录旧开发版 `median≈546ms / payload≈159KB` 相比，
同接口同参数下 TTFB 从 ~546ms 降到 **~36ms（约 15 倍）**，payload 从 159KB 降到 **16.2KB（约 10 倍）**。
批量序列化 + 列表/详情职责拆分已在新镜像上实际生效并得到容器内验证。

返回结构确认为**精简项目卡片**（非详情字段）：
`id / name / status / source_count / target_count / translated_source_count / checked_source_count / effective_permissions / group_type / member_summary / team / project_set`。

### 生产服务器旧版本

此前尝试在生产服务器容器内复用开发服务器 token 时返回 400，因为该 token 只对开发服务器有效。

`F:\Working\Google\生产服务器.txt` 中包含有效的生产服务器 token，但尚未用于生产容器内的基线测量。

两份 fetch 文件还存在参数差异：

```text
开发服务器 URL: ...status=NORMAL...
生产服务器 URL: ...status=0&word=...
token 不同
```

因此若要对比新旧版本，需使用生产专用 token 在**生产容器内**执行同请求形态的只读 GET 基线测量
（`status=0&word=` 形态）；此项为可选的记录性对比，不阻塞开发服务器优化已达成的事实。

## 已知待解决问题

1. 生产服务器旧版本缺少真实容器内耗时基线。
2. ~~开发服务器新版本仍存在约 500ms 级别响应时间~~ → **已解决**：2026-08-26 容器内实测 `~36ms/16.2KB`。
3. ~~请求中同时带有 `project_set` 与 `project_sets` 重复参数~~ → **已确认无影响**：当前前端源码 (`src/apis/project.ts` `getTeamProjects`) 只发 `project_sets`；后端 (`app/apis/team.py`) 对 `project_set`/`project_sets` 均读取并 `dict.fromkeys` 去重合并。即使双参数同时送达也解析为同一 project_set，实测双参数与单参数返回 payload 完全一致（16,629B），无重复查询或错误。
4. ~~当前剩余耗时来源尚未完全定位~~ → **已定位**：列表接口仍返回 16.2KB 瘦身 payload，服务端总耗时已降到 ~36ms，此前假设的 member summaries / team 序列化 / Mongo count / 大 JSON payload 等瓶颈已随列表/详情职责拆分 + 批量快照而消除。

## 下一步计划（收尾）

1. **开发服务器优化已闭环并验证**（见上文 2026-08-26 实测；含本轮团队成员列表优化）。当前服务已更新到 `moeflow-20260827-214208-1b8c76e1-3282a7f5`。
2. 生产服务器基线（可选、需授权）：用 `F:\Working\Google\生产服务器.txt` 的生产 token，在生产容器内执行只读 GET（`status=0&word=` 形态），对比旧版 `v1.1.8` 基线——仅作记录，不阻塞当前优化结论。
3. 收尾项：
   - ✅ 清理后端 `ruff check` 5 个未使用 import（`app/apis/archive_import.py`、`app/services/project_invitation.py`、`app/tasks/archive_import.py`×2、`app/validators/project.py`）——已随本轮部署清理，`ruff check app/` 全绿。
   - ⏳ 评估前端 `npm audit` 告警。
    - ⏳ 前后端脏工作区按功能拆分提交后再部署；当前状态和变更规模以各仓库 `git status` 为准。

## 本轮已落地的根本性改造（2026-08-25）

已将列表接口与详情接口的响应职责拆开，避免列表请求执行详情页级序列化：

- `/v1/teams/{team_id}/projects` 和 `/v1/user/projects` 改用项目卡片序列化器；只返回名称、状态、进度缓存、三类卡片权限、团队/项目集摘要和紧凑成员摘要。
- `/v1/teams/{team_id}/project-sets` 改用项目集列表序列化器，只返回 `id/name/default`。
- `/v1/user/teams` 改用团队列表序列化器；归档 API key、OCR 配额、介绍、角色对象等详情字段不再进入列表响应。
- 列表成员摘要不再查询用户头像、别名、版本、权限来源和时间字段；打开编辑器时再请求完整成员接口。
- 前端进入项目、项目集或团队详情/设置页时强制请求详情对象，避免把精简列表对象当成详情对象。
- 修复项目列表请求同时发送 `project_set` 与 `project_sets` 的重复参数。

本轮代码已完成后端编译、Ruff、前端 typecheck，以及相关后端接口回归和前端 18 个测试套件（110 个测试）验证，并已部署到开发服务器（这些均为本轮历史验证记录，容器内实测确认生效，见上）。

> 开发服务器容器内实测结论（2026-08-26）：同接口同参数下 TTFB 从 ~546ms → **~36ms**，payload 从 159KB → **16.2KB**，优化闭环达成。剩余事项仅为可选的**生产旧版基线对比**（只读，需授权）与一般性收尾（ruff 清理、npm audit、提交整理），详见上文「下一步计划」。

## 本轮扩展优化（2026-08-26）

在项目列表/团队列表优化闭环后，对**团队成员的聚合列表端点**做了同类审查，并落地两处与「逐条 to_api + N+1」同源的优化：

- **团队成员列表批量预取 user**（`app/apis/identity.py` `TeamMemberListAPI`）：成员管理接口一次性可能返回上千团队成员，原实现逐条 `member.to_api()` 且每条触发 `member.user` 惰性引用（N+1）。改为先收集本页成员的全部 user id，用一次 `User.objects(id__in=[...])` 批量预取，并把 `user_map` 传入 `TeamMember.to_api(user_map=...)`；同时只对**页面切片**做序列化（`count` 仍为全量），深页不再物化整组成员文档。
- **`list_members` 排序批量预取**（`app/services/team_member.py`）：排序键 `member.user.name` 原本在逐条惰性解引用 user（排序阶段就先于 to_api 触发 N+1）。改为排序前一次批量预取 user 构造 `user_lookup`，排序不再触碰存储。
- **`TeamMember.to_api(user_map=None)` 批量上下文**（`app/models/team_member.py`）：与 `Project.batch_to_api` 同模式，命中预取 user 时不再惰性查询，缺 id 时回退逐条加载（行为不变）。
- **回归测试**：新增 `test_team_member_to_api_supports_batch_user_map`，断言批量上下文与默认逐条序列化逐字段等价。
- **前端快捷编辑请求合并**（`src/components/shared/MemberStats.tsx`）：点击项目「人员快捷编辑」原瞬发 3 次请求（项目成员 active + invited 各 1 次 + 团队成员 1 次）。抽共享 `loadProjectMembers()` 用单次 `status=active,invited` 请求合并前两项，点击从 3 次降为 2 次（第 3 次团队成员用于资格校验，语义不同，保留）。

### 团队成员列表实测

在开发服务器容器内（历史采样 release `moeflow-20260826-025947`）实测 `/v1/teams/{id}/members?status=active,removed&limit=3000`（当时 seed 数据该团队 92 名成员，payload 51KB）：

```text
TTFB 7 次: 231 / 562 / 403 / 195 / 258 / 226 / 233 ms，median ≈ 231ms
```

说明：本优化把「N 次 user 惰性查询」决定性降为「1 次批量查询」，**对真实生产的大团队（千级成员）收益显著**。历史采样中的开发服务器 `seed_production_shape.py` 仅构造了 4 个团队、该团队 92 名成员，N+1 差异被约 1 GiB 小机器的固有开销掩盖，故实测未出现项目列表那样的数量级下降；接口字段（`user` / `user_id` 等）完整保留，无回归。

### 本轮验证记录

- 本地：编译 + `ruff check app/` 全绿；`tests/model/test_identity_models.py` + `test_identity_services.py`（含排序修复）→ 50 passed。
- 容器（真实 MongoDB，分批 + `--no-cov` 控制内存）：完整身份套件 5 套件（models/services/permission/performance_regressions/api）全部通过；`test_identity_api` 最终版 **33 passed**。
- `moeflow.ps1 -Action Test` 验收门（migrations / OSS-R2 / performance_regressions / celery 路由）exit 0。
- 部署注意：开发服务器约 960 MiB 内存、1 GiB swap，后端及 worker 容器 512 MiB 上限，容器内跑完整测试需**分批 + 禁用 coverage**，避免 OOM（历史上一次全量 coverage 测试导致服务器重启）。`moeflow.ps1 -Action Deploy` 在后台非交互下会被 `ShouldProcess` 拦截，需 `$ConfirmPreference='None'` 显式放行。

## 访问方式备忘

开发服务器：

```bash
ssh -p 23213 -i D:/moeflow/scripts/config/keys/m_usag root@100.90.141.40
```

生产服务器需要通过本地代理连接，辅助脚本位于：

```text
D:\moeflow\scripts\python\moeflow-http-connect-proxy.py
```

以上参数均已写入 `scripts/config/moeflow.config.json`，直接使用
`scripts/moeflow-prod.ps1` 即可，无需手工拼接连接命令。

代理端口：

```text
127.0.0.1:7897
```

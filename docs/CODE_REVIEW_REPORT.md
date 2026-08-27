# Moeflow 前后端代码审查报告

审查日期：2026-08-26（Asia/Shanghai）
审查工作区：`D:\moeflow`
审查对象：当前工作区中的 `moeflow-backend` 与 `moeflow-frontend`，包括未提交修改

> 状态更新（2026-08-27）：本文主体是 2026-08-26 的代码审查快照。当前后端已使用系统
> Python 3.12.5 和更新后的锁文件，Flask-APIKit 已移除；开发服务器当前 release 为
> `moeflow-20260827-214208-1b8c76e1-3282a7f5`。最新综合验收见 §4.3，本文旧测试数字
> 保留为审查当时的历史结果。

## 1. 结论

截至审查时，当前版本已经完成身份成员模型、权限服务、迁移、缩略图异步化、归档导入和前端成员管理的大部分集成工作；后端审查快照为 `474 passed, 1 skipped`，前端类型检查、Jest、lint 和生产构建均通过。最新开发服务器回归结果已在 §4.3 更新。

当前不再存在本轮已确认的 P0 代码问题，但仍不建议仅凭本地测试直接批准生产发布。剩余风险主要集中在认证 token 的浏览器存储模型、历史归档 API key 的迁移、不可逆迁移的大数据量演练，以及真实 Celery/Mongo/对象存储/provider 环境验收。

范围注记：按用户明确口径，画廊 `token` 属于业务参数，不纳入隐私信息泄露项，也不作为本报告的安全 finding。归档链路仍需保护管理员密码、归档 API key 和 provider 外部网络边界。

## 2. 审查范围

已阅读 `moeflow-backend/docs` 中的项目状态、性能、身份重构和归档导入文档，并审查以下代码范围：

- 后端模型、API、RBAC、身份服务、Celery 任务、迁移、配置和测试。
- 前端 API 封装、项目/团队/成员页面、归档导入进度、Redux store、Cookie、Vite 构建和 lint 配置。
- 认证和权限边界、外部 HTTP 请求、API key、ZIP 处理、并发更新、迁移可恢复性、列表分页排序和前后端接口契约。

两个仓库均存在大量用户已有未提交修改。本次没有清理、回滚或覆盖无关改动。

## 3. 已确认并已修复

### 3.1 归档导入

- 删除生产环境固定默认管理员密码 `123123`；缺少 `ADMIN_INITIAL_PASSWORD` 时拒绝创建默认管理员，日志不输出密码。
- 团队归档 API key 使用 Fernet 加密存储，API 只展示尾号，worker 读取时解密；保留旧明文数据兼容读取，并在设置更新时重新加密。
- provider URL 要求 HTTPS，团队自定义地址受站点 host 白名单限制，并拒绝 loopback、私有和保留 IP；provider 解析请求和归档下载均禁止自动跟随重定向。
- ZIP 校验增加总条目数、单条目大小和总解压大小限制，并在读取条目时校验实际解压字节数。
- Celery 任务携带具体 `task_id`，worker 只关联该任务并通过 queued 状态原子领取；旧消息仅按完整 gallery 参数兼容查找，不再按项目取任意最新任务。
- 归档任务对 provider 未配置、key 解密失败、下载失败、ZIP 损坏和导入异常写入终态失败状态，并清理临时目录。

### 3.2 身份、权限和容量

- 团队成员新增、恢复和旧 `User.join_team()` 路径统一进入条件容量更新；满员恢复不会绕过 `max_user`。
- 项目/团队成员使用确定性主体键、唯一索引、版本号/CAS、审计记录和幂等操作记录。
- 项目权限由身份标签服务实时计算；邀请中的成员、移除成员和外部署名不会错误获得 active 标签权限。
- 测试已迁移到当前身份契约：旧“监理”映射为 `proofreader`，翻译删除测试覆盖作者、校对标签和协调者映射后的行为。
- `Project.clean()` 已接入团体枚举校验，非法申请方式和状态不能静默保存。

### 3.3 查询、排序和迁移

- 团队成员搜索先使用 User/TeamMember 搜索投影和 Mongo 条件过滤，再批量加载引用用户，避免搜索路径逐条惰性查询。
- `m0006` 搜索投影回填改为固定批次写入。
- 项目列表、提示和翻译列表增加稳定排序键；翻译和提示写入端对 Mongo 毫秒级时间精度做严格递增处理，避免相同时间戳导致分页或修订版复制顺序漂移。
- 新增并修复满员恢复、旧加入路径、归档 ZIP 限制、任务关联、API key 加密、权限映射、排序和注册邮箱契约测试。

### 3.4 前端工程和认证传输属性

- lint 脚本改为只扫描 `src`，不再扫描 `build` 等构建产物。
- source map 和 visualizer 改为显式环境开关，默认生产构建不公开 source map、不生成可视化工件。
- 浏览器 Cookie 增加 `Secure`（HTTPS 上下文）和 `SameSite=Lax`；代码明确保留服务端 HttpOnly session 作为后续更强方案。

## 4. 验证结果

### 4.1 后端

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -q --disable-warnings`（2026-08-26 审查快照） | `474 passed, 1 skipped`，约 10 分钟 |
| `python -m ruff check app tests` | 通过 |
| `python -m compileall -q app tests manage.py` | 通过 |
| `git diff --check` | 通过 |

全量测试覆盖了归档 API/任务、身份 API/服务、邀请申请、认证、文件与修订版、迁移、缩略图、输出和缓存等路径。

### 4.2 前端

| 检查 | 结果 |
| --- | --- |
| `npm run typecheck` | 通过 |
| `npm test -- --runInBand --silent` | 19 个 suite、112 个测试通过 |
| `npm run lint` | 0 errors、269 warnings |
| `npm run build` | 通过；仍有大 chunk 警告和第三方依赖 `eval` 警告 |

当前最大构建产物约为 `index 1.33MB`、`antd 825KB`，这是性能优化项，不是本轮回归失败。

### 4.3 2026-08-27 开发服务器综合验收

开发服务器约有 960 MiB 内存和 1 GiB swap，后端与两个 Celery worker 每个容器限制
512 MiB。为避免 OOM，测试关闭 coverage、禁用 xdist，并按测试目录分组串行执行：

```text
pytest -q -o addopts= --no-cov -p no:cacheprovider --disable-warnings --maxfail=1
```

`tests/base` 51 passed（4 subtests）、`tests/api` 210 passed、`tests/model` 179 passed
（4 subtests）、`tests/other` + `tests/tasks` 35 passed，合计 `475 passed`（4 subtests）。
使用 `/root/moeflow-prod-20260816-104619.archive.gz` 在开发服务器恢复约 680,879 条文档后，
`0000` 至 `0006` 迁移全部成功；生产服务器本身未被修改。当前服务入口为
`http://100.90.141.40:5080/`。

## 5. 当前剩余问题

严重程度：P1 为近期必须处理，P2 为工程质量或运营风险。

### P1-1：长期 Bearer token 仍由 JavaScript 可读 Cookie 保存

位置：`moeflow-frontend/src/utils/cookie.ts`、用户初始化和认证 saga。

前端需要从 Cookie 读取 token 并放入 Redux/Authorization header，因此浏览器端无法设置 `HttpOnly`。当前 `Secure` 和 `SameSite=Lax` 能降低明文传输和部分跨站请求风险，但一旦发生 XSS、第三方依赖污染或恶意浏览器扩展，token 仍可被脚本读取。后端 token 默认有效期较长时，影响窗口更大。

建议：改为服务端设置的 `HttpOnly; Secure; SameSite` session/refresh Cookie，短期 access token 仅驻留内存；在架构迁移前缩短 token 有效期、支持撤销和轮换，并建立 CSP、依赖审计和 XSS 防护门禁。

### P2-1：历史归档 API key 仍可能以旧格式存在

位置：`app/utils/secrets.py`、`app/apis/team.py`、`Team.archive_api_keys`。

新写入和被设置接口触碰的 key 会加密，但为兼容历史数据，读取逻辑仍接受没有 `fernet:` 前缀的旧明文值。这样数据库备份中可能仍存在历史明文 key。

建议：提供一次性、可审计的全量 re-encryption migration；完成后关闭明文 fallback，并安排现有 key 的轮换、撤销和失败恢复方案。加密密钥应由部署 secret/KMS 管理，不能只依赖数据库备份可恢复。

### P2-2：大数据量身份迁移仍需生产规模演练

位置：`app/migrations/versions/m0004_identity_members.py`、`m0006_search_projections.py`。

开发服务器已使用约 680,879 条文档的生产归档副本完成 `m0004` 至 `m0006` 重放，验证了当前数据规模下的耗时和结果。`m0006` 已按批次回填，但 `m0004` 仍在单次迁移中构造较大的 Python 映射和更新集合。迁移 runner 有锁、校验和和失败停止，但 Mongo 写入与 migration record 之间不是事务；生产进程在中途 OOM 或网络中断时，可能留下部分回填状态。

建议：开发服务器的生产归档重放已验证当前数据规模下的耗时和结果；生产执行前仍需记录峰值内存、索引耗时和中断恢复行为，并确认快照、回滚/恢复演练结果。为大集合增加游标批处理、幂等 checkpoint、dry-run 统计和部分完成恢复说明。

### P2-3：前端 bundle 和 lint warning 尚未纳入质量门禁

当前 lint 没有 error，但有 269 个 warning，主要是 `any`、未使用 reducer 参数和非空断言。构建的大 chunk 会增加首屏下载和解析成本，第三方 `store` 依赖仍触发 `eval` 警告。

建议：按路由和功能懒加载 AI、Ant Design 和大型编辑器依赖；建立 chunk 预算；优先清理安全、React hooks、类型逃逸和未使用变量 warning；对 `eval` 依赖进行升级、替换或明确例外记录。

### P2-4：真实外部依赖验收尚未由本地单测覆盖

本地测试主要使用测试数据库、mock provider/对象存储和同步 Celery 配置，尚未证明真实部署下的以下行为：worker 重启与重复投递、provider DNS/重定向、网络超时、磁盘不足、对象存储失败、迁移中断和 key 轮换。provider host 白名单不能替代部署层 egress allowlist。

建议：将这些场景加入 staging 验收，记录任务状态转换、重试、临时目录清理、Mongo 索引和出站网络审计结果。

## 6. 正面发现

- 身份主体使用确定性 key 和唯一索引，项目/团队成员更新使用版本条件，降低重复主体和并发覆盖风险。
- 权限服务统一归一化旧数字权限和新稳定字符串权限，且可返回权限来源，便于审计和迁移排查。
- 文件对象路径不使用用户原始文件名；文件名和归档条目复用危险路径字符校验。
- 上传、缩略图、归档导入在失败路径保留或清理对象的策略较完整，任务状态不会长期停留在无解释的中间状态。
- 前端成员、身份标签、邀请、Cookie、分页排序和归档进度均有新增测试；审查快照中的前端测试数量为 112 个。

## 7. 发布建议

生产发布前建议按以下顺序完成：

1. 确认认证 token 的 JS 可读 Cookie 是否被产品安全模型接受；若不接受，先完成 HttpOnly session/refresh 改造。
2. 在 staging 对历史归档 API key 做全量加密迁移和 key 轮换，确认旧明文 fallback 可以关闭。
3. 使用生产规模数据演练 `m0004` 至 `m0006`，记录内存、耗时、索引和中断恢复结果。
4. 执行真实 Celery、Mongo、对象存储和 provider 验收，并在网络层配置 egress allowlist。
5. 将后端全量测试、Ruff、前端 typecheck/Jest/lint/build 和 bundle 预算加入 CI 发布门禁。

## 8. 最终判断

本轮代码和测试修复完成后，Moeflow 已从“回归不全绿、归档链路存在明显保护缺口”进入“本地回归全绿、剩余问题主要是认证架构和生产运营验证”的阶段。画廊 token 按约定不构成隐私风险项；当前发布决策应重点关注浏览器 Bearer token 存储、历史 API key 迁移、不可逆迁移演练和真实异步基础设施验收。

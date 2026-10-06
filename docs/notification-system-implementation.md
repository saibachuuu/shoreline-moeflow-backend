# 通知系统初版实现与验收记录

更新：2026-10-06。对应 [实施计划](notification-system-plan.md)。

## 1. 当前交付状态

用户已授权开始实现。初版主流程已落在两个仓库的 `dev`，代码默认关闭功能。2026-10-06 根据用户追加授权，已部署开发服务器并启用站内通知，完成真实 MongoDB／RabbitMQ／Beat／浏览器验收；开发环境两类邮件开关仍关闭，SMTP 隔离。**没有修改生产、发送真实邮件或推送 Git 远端。** 具体 release、资源、测试与回滚点见 [开发部署验收记录](notification-system-dev-deployment.md)。

### 已实现

- 核心通知模型、站内收件记录、邮件 outbox、偏好、团队策略、审计、幂等、版本检查和后台恢复扫描，不依赖可选模块。
- 系统／团队／项目通用人工公告：指定用户、条件筛选、全员、服务端预览、二次确认发送、草稿、手动定时、失效时间。没有阶段完成按钮／事件，也没有个人消息生产者或公开个人发送 API。
- `/dashboard/notifications`：分类、搜索、未读、归档、详情、标为已读／未读、批量已读、分类邮件偏好，以及既有邀请／申请页链接。
- `/admin/notifications`：跨范围查看所有普通通知（包括团队／项目私有内容），搜索与筛选；只允许创建站点通知。撤回必须填写理由、二次确认并提交当前版本，留下审计；不允许管理员重写他人消息。管理浏览不修改收件人的已读状态。
- `/dashboard/teams/:teamID/setting/notifications`：团队通知及创建者专属发送权限策略；项目设置在“基础设置”后提供同级 `setting/notifications` 标签，保留当前路由前缀。
- 普通发件管理：本人发送的通知；范围创建者还能管理该范围内的通知。管理员发送策略不等于能查看其他发送者的普通发件详情。全站权限独立校验，不能替代团队／项目发送权限。
- 桌面“我参与的项目”下方的同风格通知菜单项；移动端项目／团队／通知／我四栏；保留主题和 CDN 设置，不新增移动端顶部铃铛。
- 受限 BBCode 安全节点、纯 React 展示、HTTP(S) 链接检查、最多 5 个项目名片；按每个读者当前权限解析名片，不可访问时不返回名称。发送者正文中的文字不能自动脱敏。
- 收件列表、详情、未读数和邮件投递复核接收资格；退出、移除、删除范围、撤回和失效后不返回私密内容。活动通知页面通过按账号共享的同步请求每 20 秒复核；重新聚焦去重刷新，隐藏页面暂停。已收到的邮件与已经看见的内容不能撤回。

## 2. 邮件迁移的实际边界

| 邮件 | 初版实现 |
| --- | --- |
| 校对稿 | 启用通知功能后，原业务入口只创建 `project / proofread_feedback`，不再同时排入旧邮件任务。保留原校对权限、目标语言／文件范围、差异模板、项目翻译成员、默认复制给自己及仅自己接收的回退。每个收件人一封，不使用 Cc 暴露名单。 |
| 普通人工通知 | 永远创建站内记录；只有发送者显式勾选邮件时才创建邮件投递记录。接收偏好、当前资格、邮箱验证、邮件开关和 SMTP 收件保护仍可阻止发送。私有团队／项目邮件只含登录查看提示；站点通知可以包含正文。 |
| 确认邮箱／重置邮箱／重置密码 | 不创建普通通知或普通 outbox；启用功能后使用 `security_email` 专队列。任务参数仅验证码记录 ID 与带密钥的代际摘要，不包含验证码明文；执行／重试时重新确认记录仍存在、未过期且未被替换，再渲染模板。 |
| ERROR 运维邮件 | 不进入普通通知或上述队列；启用功能后使用独立同步受保护 SMTP，保留 `ENABLE_LOG_EMAIL`，每进程最多 5 次／分钟，不依赖 Celery 或通知数据库，也不递归发告警。 |
| 已排队旧邮件 | 保留 `tasks.email_task` 的旧位置参数（包括 Cc）消费者，兼容部署前排队任务。功能关闭时校对稿与安全邮件使用旧路径，不进行历史通知补发。 |

校对稿详情以安全纯文本展示完整反馈，不把历史模板 HTML 直接注入网页。发送确认提示邮件包含详细校对内容及发送者邮箱 Reply-To。校对人员可以管理自己发出的反馈，不因此获得通用项目公告发送权。

验证码及完整目的邮箱不再写入验证码日志，DEBUG／TESTING 也不输出验证码。成功确认新邮箱会写入邮箱摘要验证标记（不含验证码）。认证成功文案改为“请求已受理”，不再承诺已经送达。

### 邮箱验证的重要兼容决定

现有 User 模型没有足以证明历史邮箱已经验证的标记，因此默认 **不信任所有历史邮箱**：

- `NOTIFICATION_TRUST_EXISTING_EMAILS=False` 时，普通邮件要求 `NotificationVerifiedEmail` 中存在当前邮箱的 SHA-256 摘要。
- 该标记仅由启用功能后的成功 `CONFIRM_EMAIL` 验证写入；不会根据登录、请求验证码或管理员输入邮箱推断已验证。
- 历史用户仍有站内通知，但邮件可能显示 `unverified_email` 而跳过。仅在运营者明确确认历史数据可信后才可启用 `NOTIFICATION_TRUST_EXISTING_EMAILS=True`。本轮没有执行历史回填。
- 验证码邮件不受普通通知偏好或上述验证标记限制，否则无法完成首次验证；仍遵守用户邮件开关及 SMTP 收件保护。

## 3. 实际 API 与格式

公共人工发布只接受 `category / scope_id / title / body / audience / email / draft / publish_at / expires_at`；分类仅 `system / team / project`，正文为基础 BBCode。业务类型、来源、actor、HTML、投递状态不能由客户端指定。日期必须为带时区 ISO 8601。标题最多 200 字，正文最多 10000 字，定时最多一年。

| 接口 | 说明 |
| --- | --- |
| `GET /v1/me/notification-capabilities` | 开关、`can_send`、`can_view_sent`、全站管理与团队策略权限；可传 `category / scope_id`。 |
| `GET /v1/me/notifications`；`.../unread-counts` | 本人可见收件箱、分类未读数。 |
| `GET/PATCH /v1/me/notifications/:id`；`POST .../mark-read` | 详情、本人已读／归档操作；批量已读可传分类，以服务端请求开始时间为截止。 |
| `GET/PUT /v1/me/notification-preferences` | 分类邮件偏好，版本检查。 |
| `POST /v1/notifications`；`.../preview` | 普通公告发布／预览。发布使用 `Idempotency-Key`，返回 202 表示受理。 |
| `GET /v1/notification-recipients` | 当前发送范围的候选用户搜索、游标分页，不返回邮箱。 |
| `GET /v1/notifications/sent`；`GET/PATCH /v1/notifications/:id` | 范围发件历史、详情、草稿／未执行定时修改。 |
| `POST /v1/notifications/:id/revoke` | `confirmed: true / reason / version` 加幂等键；不可恢复已撤回内容。 |
| `GET/PUT /v1/teams/:id/notification-policy` | 团队创建者修改团队管理员／项目管理员发送策略。 |
| `GET /v1/notification-project-cards`；`POST .../resolve` | 项目搜索（游标分页）／至多 5 个名片的当前可见投影。 |
| `GET/POST /v1/admin/notifications`；`POST .../preview` | 全站检索、站点通知创建／预览。POST 不接受团队或项目分类。 |
| `GET /v1/admin/notification-recipients` | 站点候选用户搜索和分页。 |
| `GET /v1/admin/notifications/:id`；`POST .../:id/revoke` | 全站详情、带理由及版本的二次确认撤回。没有管理 PATCH。 |

草稿发布合并在 `PATCH /v1/notifications/:id`：发送完整内容、`draft: false` 和当前 `version`。未单独实现草案 `/publish` 路径。PATCH 遇版本冲突应先 GET 核对已提交结果，不盲目重复提交；已被扫描器租用的定时消息不能同时改写。

站点通知 `audience` 支持 `manual / all / condition`；条件可选 `site_roles` 与 `team_ids` **或** `project_ids`。团队／项目支持 `base_roles`、资质／职位；手选不允许越界、空名单或混合全员条件。条件内部 OR、不同维度 AND。

分页：`limit` 1–100，返回 `items / next_cursor`；普通列表游标降序，候选用户／项目游标升序。候选 UI 支持继续加载，不下载全站用户。消息关键词最少 2 字、最多 100 字，24 位对象 ID 可精确定位；标准化 Unicode 双字索引先筛候选，再做字面包含匹配，用户不能提交正则。返回纯文本匹配片段，不返回原始 HTML。

全站筛选：`category / team_id / project_id / actor_id / source / event_type / state / revoked / expired / delivery_state / from / to`。搜索当前覆盖标题和普通正文（校对稿覆盖通知摘要，不检索完整历史邮件模板）。普通发件接口不能借筛选参数扩大已授权范围。

个人底层契约为 `register_source("module:<name>", validator)` 与 `publish_personal(actor, source, data, key)`；仅限服务端调用，注册来源负责校验其接收关系，没有公开注册或生产接口。

## 4. 数据库、扫描器与 SMTP

新增集合：`notification`、`notification_audience_chunk`、`notification_receipt`、`notification_delivery`、`notification_preference`、`notification_policy`、`notification_audit`、`notification_verified_email`、`notification_throttle`。

索引定义集中于 `app/models/notification.py`：发布幂等键、通知＋分块号、通知＋接收用户、偏好用户、策略团队、审计幂等键均有唯一索引；另有扫描状态／时间、范围与发送者查询、收件游标、双字检索索引。只有限流计数器有 2 分钟 TTL；业务记录没有 TTL 删除。没有添加版本迁移，没有修改迁移执行器、既有迁移文件或历史业务数据。

```shell
# 先列出集合；不启动邮件扫描
python manage.py notification-indexes
# 初始化仅属于本功能的新索引
python manage.py notification-indexes --apply
```

首次发布先保存意图，不依赖一次 Celery 入队成功。Beat 每 10 秒调用 `tasks.notification_scan`：

- 每块 100 人、每轮最多处理 4 块；默认受众上限 20000，手选上限 1000。固化名单后不回填后来加入的人。
- 租约与唯一键保证重复扫描不重复建站内记录／邮件记录；分块已写但检查点未前移时可恢复。
- 每轮至多扫描 10 条意图、50 条待投邮件；发送前检查撤回、期限、发送者权限、接收资格、当前邮箱与偏好。迟到超过 1 小时的定时消息回到待处理草稿，不自动补发。
- 邮件 `pending / leased / accepted / retry_wait / skipped / failed / unknown / cancelled` 分开记录。明确瞬时错误退避重试，最多 5 次；租约过期或 DATA 阶段断线可能已经发出，记 `unknown`，不自动重发。
- `accepted` 仅指 SMTP 接受，不表示进入用户收件箱。撤回阻止后续投递，不能召回已跨过 SMTP 边界的邮件。管理详情展示聚合状态及原因，不公开原始接收邮箱。
- 标准 SMTP_SSL／STARTTLS 验证证书；稳定 Message-ID，每封只有一个 To，没有 Cc，不把底层异常及凭据暴露给普通用户。

### 配置（见 `.env.sample`）

| 配置 | 默认／用途 |
| --- | --- |
| `ENABLE_NOTIFICATIONS` | `False`；关闭时入口隐藏，新 API 拒绝发布，旧邮件路径保留。 |
| `NOTIFICATION_MAX_AUDIENCE` | `20000`；用于限制当前受众枚举，超限拒绝而非截断后误报全员成功。 |
| `NOTIFICATION_TRUST_EXISTING_EMAILS` | `False`；上文的历史邮箱信任开关。 |
| `NOTIFICATION_EMAIL_ALLOWLIST` | 空；逗号分隔明确允许的测试邮箱。 |
| `NOTIFICATION_EMAIL_UNRESTRICTED` | `False`；非调试环境才可显式允许普通实际地址。DEBUG／TESTING 即使打开此项也必须命中白名单。 |
| `ENABLE_USER_EMAIL / ENABLE_LOG_EMAIL` | 原有两个独立开关保留。 |
| `EMAIL_REPLY_ADDRESS / EMAIL_ERROR_ADDRESS` | 各读取自己的环境变量，未设置时兼容回退 `EMAIL_ADDRESS`。 |

**新保护只约束新邮件适配器。旧任务消费者及关闭通知功能后的旧路径仍是旧规则**：开发部署不能仅设置新白名单就以为历史排队任务安全，必须同时隔离 SMTP、核查旧队列及关闭不需要的邮件开关。

通用部署形态（开发环境已采用三个受限资源的 Compose 服务，具体名称见部署记录）：

```shell
celery -A app.celery worker -Q notification --concurrency=1 --hostname=notification@%h
celery -A app.celery worker -Q security_email --concurrency=1 --hostname=security_email@%h
celery -A app.celery beat
```

仅一个 Beat；保留旧任务需要的消费者。不要只启动 HTTP 后端然后打开功能：缺少扫描器会令人工／定时通知停在待处理，缺少安全队列消费者会影响真实邮箱验证。`python manage.py notification-scan` 只用于一次受保护的维护扫描，不能替代 Beat；它可能投递邮件。

## 5. 本地验证

全部后端用隔离 `mongomock://localhost/*_test`，显式关闭两种真实邮件开关。SMTP 测试只连接本机临时 TLS SMTP 服务，临时证书不入库。

- 通知接口／可靠性／内容／权限／SMTP、旧校对稿、认证、验证码、站点管理员、身份权限回归：**100 passed，11 subtests passed**；随后补充跨域幂等键预检测试，通知接口独立复测 **36 passed**。
- 覆盖重复并发发布、范围隔离、全站读／撤回、版本冲突、受信个人接口、离队／封禁／权限失效、定时、中文搜索和游标、分块与 outbox 故障恢复、不可重试未知邮件、安全验证码代际和日志脱敏、旧排队邮件兼容。
- 前端类型检查、通知新增文件 ESLint、React 安全渲染测试 **4 passed**、生产构建通过。构建仍有依赖 `store/json2` 的 eval 和既有大 bundle 警告。
- 物理移除可选模块目录的**隔离副本**：后端启动确认 `modules: none` 且核心通知路由存在，通知测试 **36 passed**；前端零模块类型检查与生产构建通过。没有删除工作仓库中的模块。
- 本机真实前端＋Flask／mongomock 的浏览器流程：全站中文搜索、定向用户预览和发送、异步站内收件、320px 四栏导航和详情、团队／项目设置入口、必填理由的二次确认撤回、收件人不可读取已撤回内容；检查无页面 JS 异常。截图在工作区 `artifacts/notification-browser/`，包含测试令牌的 fixture 不提交。

可复现测试命令（PowerShell，运行前仍需确认只连接测试库）：

```powershell
$env:MONGODB_URI='mongomock://localhost/moeflow_notification_test'
$env:ENABLE_USER_EMAIL='False'; $env:ENABLE_LOG_EMAIL='False'; $env:TESTING='YES'
python -m pytest tests/api/test_notification_api.py tests/api/test_notification_smtp.py tests/api/test_send_proofread_draft_api.py tests/api/test_auth_api.py tests/api/test_v_code_api.py tests/api/test_site_admin_permissions.py tests/model/test_v_code_model.py tests/model/test_identity_permission.py -q -o addopts= --disable-warnings
```

## 6. 尚未关闭的上线门槛／限制

1. 开发环境已完成真实 MongoDB 4.4 的 10000 通知／1000 收件记录有界查询测试，以及消费者停止／重启、Beat 重启和重复扫描恢复测试；**尚未完成完整生产规模压力测试与所有 SMTP 故障边界的进程级注入**。现有接收资格过滤会逐批检查用户收件箱，未读数须扫描本人未读收件记录；不是已经验证过大用户历史量的常数时间统计。开发测试命中双字索引，但生产拓扑、更多私有范围及长期历史量仍须实测，不能把开发有界样本当成完整容量证明。
2. 安全邮件已独立队列且重试检查有效期，但仍依赖既有 VCode 记录＋Celery 投递，**未新增独立安全邮件持久化 outbox**。broker 写入失败会使当前验证码发送请求失败；没有像普通通知那样的 DB 周期补偿，也没有对 SMTP unknown 提供后台人工重试 UI。
3. 普通通知搜索不包含完整校对稿模板内容；站点接收范围／管理筛选中的团队、项目目前使用 ID，后续可改为实体选择器。预览显示总人数及首 100 人摘要，完整候选名单由独立分页接口搜索。
4. 通知来源注册、历史邮件验证策略、发送上限和每进程告警限流已给安全默认值；上线前仍需确认实际运营规则。禁用模块后不应继续执行其待发布来源；已存个人消息的业务接收关系由来源契约负责定义。
5. 开发环境消费者／Beat、资源限制、备份与邮件保护已落地并完成验收。服务器只有约 960 MiB RAM，swap 已满，当前小规模验收无 OOM，但上线更大范围前仍需容量评估、长期监控与告警。生产部署、外发邮件、历史邮箱信任和安全邮件持久化补偿不在本次开发部署中启用。

当前可以直接在开发站点验收站内通知；它不是已经开放真实外发邮件或完成生产全量压力验收的通知服务。


## 7. 交互复审改进（2026-10-06）

根据开发站点的实际使用反馈：

- 桌面通知菜单复用 `ListItem`，紧跟“我参与的项目”，含选中状态和未读徽标；移动四栏不改。项目通知管理复用 `NavTab`，紧跟“基础设置”；团队通知也改为同风格标签，不再把按钮塞进标签栏。
- 页面复用 `ContentTitle` 和主题变量，分开标题、工具栏、筛选、列表；高级管理筛选折叠并用网格对齐。消息标题改用正常正文色，未读以圆点和字重区分；发件人、分类、时间分层显示。编辑表单限定宽度、日期字段对齐，团队权限策略折叠展示。
- 新增 `GET /v1/me/notification-sync`。单个响应合并当前账号全局未读数、当前范围权限及活动收件箱／管理列表／详情；使用与原端点相同的授权和投影。列表筛选不会改变全局未读数。请求带上次 `revision`，重新授权并计算响应摘要后，未变化只返回 `unchanged / revision`；响应 `Cache-Control: no-store`，无共享缓存。
- 前端按账号和 token 共用一个请求／计时器：活动通知页 20 秒，其他页面徽标 60 秒；隐藏暂停、聚焦去重、错误指数退避、切换账号／筛选取消旧请求。取消原来的独立 capabilities+counts 和页面轮询。
- 无变化不发布新前端快照；后台同步不清空列表、不触发 loading，不重置已加载页和滚动位置。有新消息先显示“有新消息，点击查看更新”，用户确认才插入；已有行状态静默更新，详情权限失效时清除旧内容。显式加载更多及首次加载仍显示等待状态。
- 小批量名单固化后直接进入同轮站内投递，避免额外等待一个 Beat 周期；仍保留每轮 4 块的预算、租约、唯一键和恢复检查。Beat 间隔调整为 10 秒。不是 WebSocket/SSE 实时推送，网络／负载和页面轮询仍可能产生可见延迟。

本地新增同步控制器测试覆盖合并请求、304 等价的 unchanged 分支、聚焦去重、后台暂停、旧响应取消、账号销毁、失败退避与写入后合并刷新。浏览器验证收件箱和全站管理在一个 20 秒周期内各只有一次 sync 请求、没有额外 capabilities/count/list 请求、没有列表转圈且 DOM 节点保留；新消息点击提示后展示。截图位于 `artifacts/notification-ui-review/`。服务端增加条件同步、全局未读独立于筛选、撤回／管理隔离和单轮投递测试。


### 本轮开发站点更新结果

交互复审版本已部署：`moeflow-20261006-230557-fe19f9f3-168245a5`（前端 `fe19f9f3`、后端 `168245a5`）。本地后端 **105 passed + 11 subtests**，前端 **9 passed**，类型检查／构建／零模块前端构建通过。实际开发域名浏览器验证导航、共享同步、无列表 loading／DOM 重建、新消息确认展示、移动端与暗色主题通过；测试数据已按专用 IDs 清理。

按用户最新要求，开发机不再做备份。重连时服务器显示刚重启（不是由本次助手执行），原备份进程已不在；未在恢复连接后继续备份或重新导出。部署前停止 Beat／消费者释放内存，保留 HTTP、MongoDB、RabbitMQ，切换时逐个启动 Python 服务，不重建消息代理。服务器只做轻量真实 HTTP／队列／浏览器验证，没有重复全量数据库测试或容量造数。

部署后短时资源采样：所有容器 `running / restarts=0 / oom=false`，四个队列积压 0、消费者各 1；可用内存约 235 MiB、swap 约 859/1023 MiB，CPU 空闲 93%–97%，内存 PSI full avg10 约 0.09%。这是短时健康观测，不是长期或高并发容量保证。邮件继续关闭，原用户消息不删除，生产与 main 未修改。详见开发部署记录 §6。

# 通知系统开发部署与服务器验收

日期：2026-10-06。用户在初版实现后明确要求“部署到开发服务器上，顺便也在服务器上测试一遍”。本次仅操作开发环境，没有修改生产或推送 Git 远端。

## 1. 开发访问与版本

- **验收入口：<https://moedev.usag.cc/>**，该 Cloudflare 入口已完成真实浏览器流程。
- 通知中心：<https://moedev.usag.cc/dashboard/notifications>。
- 全站管理：<https://moedev.usag.cc/admin/notifications>，需站点管理员。
- 团队／项目入口仍在各自设置中。没有开通个人生产页面或阶段完成入口。
- 另一个既有直连域名为 `https://m.usag.cc/`：源站本机使用有效 TLS 验证通过，但本次执行端直连超时，浏览器验收走上述 Cloudflare 入口；没有为此改 DNS、防火墙或 Cloudflare 配置。
- `5080` 是源站 Nginx 的上游端口，不能把 `100.90.141.40:5080` 当作外部通用入口。

| 项目 | 值 |
| --- | --- |
| Release | `moeflow-20261006-203857-7b1fa654-43917476` |
| 前端源码 | `dev@7b1fa6542a5b78be10fcddb96702cbfa83d9f448` |
| 后端源码 | `dev@43917476eaaf87d4934e03678ef82e95f1a9637c` |
| 源码状态 | `source_mode=clean`；本地暂存构建后上传，不在服务器临时挂载源码。 |
| 运行路径 | `/root/moeflow-current` → `/root/moeflow-releases/moeflow-20261006-203857-7b1fa654-43917476` |
| Compose | `/root/moeflow-deploy-20251003/docker-compose.yml` + `docker-compose.local-code.yml` |
| 主数据库 | 开发 MongoDB 4.4.1，`moeflow`；本次未新增迁移版本。 |

验收文档的后续提交不改变上表已经部署的业务源码版本。`scripts/moeflow-dev.ps1 -Action Verify -Environment dev` 通过，镜像与 release 一致、无源码／build 覆盖挂载，Nginx `/api` 指向当前后端。前后端 `main` 未修改。

## 2. 部署顺序与保护

1. 备份旧 release、独立镜像标签、Compose／env、Nginx 配置和开发数据库。备份放在常规部署清理范围之外。
2. 开发 `.env-backend` 保持 `ENABLE_USER_EMAIL=False`、`ENABLE_LOG_EMAIL=False`，将 `EMAIL_SMTP_HOST=127.0.0.1`、`EMAIL_SMTP_PORT=1`。即便有旧邮件消费者，也不能连接真实 SMTP。白名单为空，unrestricted=False，历史邮箱默认不信任。
3. 使用已有部署脚本、干净的 dev 提交、`-SkipMigrations` 构建／上传新版本；旧迁移仅检查一致性。单独执行 `notification-indexes --apply` 初始化 9 个新集合的索引，没有修改版本迁移文件。
4. 部署通知／安全邮件消费者与单实例 Beat，在功能关闭时先验证启动与邮件保护。
5. 服务器隔离回归通过后，将开发 `ENABLE_NOTIFICATIONS=True`，重建后端和消费者，最后重启前端，避免 Nginx 缓存旧后端 IP。
6. 实际站内投递、浏览器和重启恢复验收完成后，仅清理专用测试数据；其他用户在开发站点新发的消息不删除、不撤回。

### 新增常驻服务

| 服务 | 职责 | 资源 |
| --- | --- | --- |
| `moeflow-celery-notification` | `notification` 队列，普通通知扫描及投递 | concurrency=1、prefetch=1、256 MiB、0.25 CPU |
| `moeflow-celery-security-email` | `security_email` 专队列 | concurrency=1、prefetch=1、192 MiB、0.20 CPU |
| `moeflow-celery-beat` | 每 30 秒调度扫描，单实例 | 128 MiB、0.10 CPU |

三个服务均使用本次后端镜像、`restart: unless-stopped`，不发布 HTTP 端口。两个原有 `default / output` 消费者保留；通知和安全邮件队列各实测有 1 个消费者。

首次生成 overlay 时发现仅在 overlay 内 `extends: service: moeflow-backend` 不会获得基础文件的完整环境，新增服务因缺少 `SITE_NAME` 未能启动。已在服务器及本地部署脚本修正为**显式 `extends.file` 指向基础 Compose**，同时清除继承的公开端口、覆盖为正常 storage/logs 挂载；修复后才启用功能。最终容器运行正常，没有持续重启或 OOM。

## 3. 服务器测试结果

### 3.1 真实 MongoDB 回归

在一次性测试容器安装测试依赖，单进程执行；URI 明确指定 **`moeflow_notification_acceptance_test`**，同时关闭真实邮件并使用本地 storage。没有对开发主库执行测试框架的 reset。

结果：**101 passed，11 subtests passed，139.49 秒**。

范围：

- 通知 API、权限、幂等、并发、中文搜索、游标、内容安全、偏好和撤回。
- 分块写入／检查点与 outbox 故障恢复、失效资格、定时、旧邮件任务兼容。
- 校对稿原接口和新迁移路径，验证码代际／日志脱敏／受保护发送。
- 认证、验证码、站点管理员权限及身份权限回归。
- 4 项真实 loopback TLS SMTP 测试：接受、拒收、临时失败及不确定断线；仅连接测试容器本机临时 SMTP，没有外部邮件服务器或真实收件人。

测试依赖不写入运行服务镜像，一次性测试容器与该测试数据库均已清理。

### 3.2 实际 HTTP + RabbitMQ + Beat

使用四个随机密码的临时用户（owner/member/admin/outsider）、独立测试团队／项目，邮箱统一为 `.invalid`。不使用现有用户发起测试通知。

通过：

- 服务端选人预览 → 实际 HTTP 受理 → Beat 异步固化名单 → 站内收件。
- 同幂等键重放只有一条通知；全站管理员不能代发测试团队消息；非收件人详情 404。
- 站点通知仅指定一个测试用户；项目通知到期才由后台投递。
- 待发通知撤回后没有创建收件记录。
- 管理员跨范围查看不改变收件人未读数；本人已读／未读操作生效。
- 从项目移除测试成员后，旧通知详情立即不可访问。
- 请求项目邮件产生 `skipped / channel_disabled`，没有 `accepted` 投递。
- 通过无效／不存在的验证码记录任务验证安全邮件队列实际消费；任务统计确认安全队列与扫描器执行，不产生验证码或普通通知泄漏。

### 3.3 进程重启与补偿

- 停止通知消费者，实际 HTTP 提交仍成功保存意图，此时没有站内收件记录。
- 启动消费者并重启 Beat 后，在等待窗口内自动完成投递。
- 额外发送两次扫描任务，收件记录仍只有预期 2 条，没有重复扇出。
- 恢复后检查消费者及单实例 Beat 均在线，队列无积压。

这验证了开发环境中的消费者离线／重启补偿，不等于已经注入所有 SMTP DATA 边界的进程崩溃场景。

### 3.4 真实数据库有界查询样本

专用 **`moeflow_notification_query_test`** 中构造 10000 条通知、1000 条收件记录，实际调用 API，每项 10 次，完成后删除该专用库。

| 测试 | 中位数 | 最大值 |
| --- | ---: | ---: |
| 全站中文搜索，20 条一页 | 15.6 ms | 34.1 ms |
| 收件箱首 20 条 | 63.1 ms | 184.8 ms |
| 1000 条未读统计 | 572.4 ms | 946.9 ms |

该中文查询 `explain` 使用 `search_tokens_1` 的 IXSCAN，检查 40 个键／文档，返回 20 条，没有扫描全量 10000 文档。该数据只代表开发服务器的有限、缓存逐渐热起来的样本；更多私有范围、正文规模、并发和历史积累仍需进一步容量验证。未读统计当前随本人历史记录增长，不是常数时间设计。

### 3.5 浏览器验收

通过公开开发域名 **`https://moedev.usag.cc`** 使用实际构建与真实 API，没有替换 API 或 runtime config：

- 全站中文检索、手选接收人、发送前预览、确认发送、真实异步站内收件。
- 320px 移动端项目／团队／通知／我四栏和详情页，无横向溢出。
- 团队设置和项目基础设置的通知管理入口。
- 撤回必须填写理由并二次确认，撤回后收件人无法再次读取正文。
- 全流程没有页面 JavaScript 异常。

本地截图：`D:\moeflow\artifacts\notification-dev-deploy\` 下的 `admin-desktop.png`、`send-preview.png`、`inbox-mobile-320.png`、`detail-mobile-320.png`、`team-settings.png`、`project-settings.png`、`revoke-confirmation.png`、`admin-mobile-320.png`。测试 token 不提交，测试账号已删除。

## 4. 数据清理与剩余限制

- 只按记录的临时用户／通知／团队／项目 IDs 清理，不清空开发主库，不调用项目 storage 清理流程。
- 原有用户、团队、项目数量和迁移记录摘要与测试前一致；其他用户在验收期间发出的消息保留。
- 临时回归库、查询样本库、一次性测试容器均已删除。
- 功能已开，**业务邮件与运维邮件仍关闭**。安全邮件真实外发也未打开，和此次部署前的开发邮件状态一致。
- `NOTIFICATION_TRUST_EXISTING_EMAILS=False`；没有历史邮箱信任回填。以后开邮件前需另行确认历史邮箱策略和真实 SMTP 配置。
- 安全邮件没有独立 DB outbox，broker 入队失败的持久化补偿仍是已知保留项；见实现记录。
- 主机约 960 MiB RAM；最终观测约 211 MiB available、1 GiB swap 已满。当前低并发验收无 OOM，但没有承诺更大负载或长期容量。不要直接提高消费者并发或并行运行大型测试。

## 5. 回滚点

备份目录：`/root/moeflow-notification-rollback-20261006`，权限仅 root 可读。

包含：

- 旧 release `moeflow-20261005-193049-88330ad3-963c59da` 的完整发布内容。
- `moeflow-notification-rollback:backend-20261006`、`moeflow-notification-rollback:frontend-20261006` 独立镜像标签，不受常规旧镜像清理规则影响。
- 旧 Compose／env、Nginx 配置。
- 开发库 `mongodump --archive --gzip` 与 SHA-256，压缩完整性及摘要校验通过；未执行全库恢复演练。
- `rollback.sh` 已做 Bash 语法检查，**未执行**：停止新增消费者，恢复原镜像标签与配置、旧 release symlink，重建原后端／消费者／前端。

回滚脚本不自动恢复整库，避免覆盖部署后的用户操作；数据库恢复必须另行审查。通知新增集合不会妨碍旧代码启动，也不需要回滚／改写既有迁移版本。

# 从画廊 URL 导入项目（归档 API 方案）

> 状态：**已实施并在开发服务器验证**（后端 + 前端 + 测试，多轮部署与回归通过；生产服务器未修改）。
> 目标：创建项目时输入画廊 URL，后端通过「第三方档案 API」下载归档 zip 并导入项目，
> 替代人工"另存 zip → 导入压缩包"流程。

> 当前运行基线：后端使用系统 Python 3.12.5，依赖由 `requirements.txt` 和
> `requirements-dev.txt` 锁定。当前开发服务器约有 960 MiB 内存和 1 GiB swap，后端
> 与两个 Celery worker 每个容器限制 512 MiB；完整测试必须分批、串行并关闭 coverage。
> 生产归档 `/root/moeflow-prod-20260816-104619.archive.gz` 仅用于开发服务器迁移和
> 回归验证。

## 1. 流程总览

```
创建项目表单（新增「画廊 URL」输入框，可选）
  │
  │ 前端解析 URL → gid + token
  │（正则参考 eh-tools gallery_push_parser.py：/g/(\d+)/([A-Za-z0-9]+)）
  │
  ├─ 无 URL：走现有 createProject（行为不变）
  └─ 有 URL：创建项目（携带 gallery_url/gid/token）→ 触发归档导入任务
            → 跳转项目页，前端轮询任务进度

Celery 任务 archive_import_task：
  ├─ 读取团队配置 archive_api_keys（管理员在团队设置中填写）
  ├─ 调第三方档案 API（gid + token + key）→ 归档 zip 直链（+ 标题等元数据）
  ├─ 流式下载 zip（大小上限 + 超时）
  ├─ zip 完整性校验（移植 eh-tools archiver.py 的校验逻辑）
  ├─ 解包 → 逐张 project.upload（复用现有 OSS 上传/缩略图/权限体系）
  └─ 更新任务状态：解析中 → 下载中(%) → 校验中 → 导入中(x/y) → 完成/失败
```

## 2. 关键决策（已与需求方确认）

| 决策点 | 结论 |
| --- | --- |
| 下载/解析在哪做 | **moeflow 服务内部**（Celery worker），不引入外部下载器进程 |
| 中间件 | **标准异步部署需要 RabbitMQ**——Celery worker 通过 broker 调度归档任务并直接 HTTP 调档案 API；测试模式可同步执行，broker 不可达时任务有同步回退 |
| 档案来源 | **第三方画廊档案 API**（返回归档 zip 直链），非 e-hentai 官方 api.php |
| 鉴权 | **团队级 API key**，管理员在团队设置中填写一次；用户侧零登录态 |
| 用户输入 | **画廊 URL**，前端解析出 gid/token 后提交 |
| 时间/直观性 | 异步任务 + 前端进度轮询（归档为单文件下载，10s~1min 量级） |

## 3. 前端改动

### 3.1 创建项目表单（`moeflow-frontend/src/components/project/ProjectCreateForm.tsx`）
- 新增「画廊 URL」输入框（可选；团队未配置 API key 时隐藏或禁用）。
- 提交时前端解析 URL：
  ```ts
  // 兼容域名：exhentai.org / e-hentai.org / ex.fangliding.eu.org 等
  const match = url.match(/\/g\/(\d+)\/([A-Za-z0-9]+)/);
  if (!match) → 表单报错「无法识别画廊链接」
  ```
- `api.project.createProject` 的 data 增加 `galleryUrl`（或直接传解析后的 `gid`/`token`）。

### 3.2 项目页导入进度
- 创建成功跳转项目页后，若触发了归档导入，显示进度条：
  - 轮询 `GET /v1/projects/<id>/import-task` → `{status, status_name, stage, total_pages, completed_pages, error, gid, token, gallery_url, dismissed, ...}`
  - 阶段文案：解析中 / 下载中 x% / 校验中 / 导入中 x/y / 完成 / 失败(原因+重试按钮)
- 成功/失败横幅均提供「**不再提示**」按钮：调用 `POST /v1/projects/<id>/import-task/dismiss`，将该项目所有非运行中任务的提示标记为关闭（存库，换终端/设备不再出现；之后手动再次触发导入，新任务完成后提示会重新出现，可再次关闭）。

## 4. 后端改动

### 4.1 团队配置（`app/models/team.py` + 团队设置 API）
- `Team` 增加字段：`archive_api_keys`（`ListField(DictField)`，每项 `{key, remark?, enabled?}`）——**每个团队可配置多个 NEW_API key**。
- 团队设置接口：仅管理员可读写；普通成员不可见（防泄露）；**返回时脱敏**（只显示尾号，如 `****abcd`）。
- key 使用策略：解析归档时**按序尝试**，第一个成功返回 `archive_url` 的 key 生效；401/error 换下一个（移植 eh-tools 多 key 轮换思路，暂不做余额择优/扣费——无余额快照来源）。
- key 存储：使用 Fernet 加密后保存，API 只返回尾号和备注；默认从 `SECRET_KEY` 派生加密 key，也可通过 `ARCHIVE_API_KEY_ENCRYPTION_KEY` 使用独立 Fernet key。旧明文值仅为兼容历史数据而读取，触碰团队设置时会重新加密。
- **API 基址团队化**：`Team.archive_api_url`（可空）优先于全局 `ARCHIVE_PROVIDER_API_URL`；两者都空则任务 FAILED「未配置档案 API 地址」。团队设置页可单独配置/清空。

### 4.2 归档导入任务模型（新 `app/models/archive_import.py`，仿 `app/models/output.py`）
```python
class ArchiveImportStatus(IntType):
    QUEUED = 0
    RESOLVING = 1   # 解析画廊/获取直链
    DOWNLOADING = 2 # 下载 zip
    VALIDATING = 3  # 校验 zip
    IMPORTING = 4   # 导入图片
    SUCCEEDED = 5
    FAILED = 6

class ArchiveImportTask(Document):  # 实际字段/缩写见 §8
    project, user, gid, token, gallery_url
    status, stage, total_pages, completed_pages, error, zip_url
    dismissed, create_time, update_time
```
- 每个项目仅一个进行中任务（触发时对运行中任务去重；失败/完成后可再次触发，失败可手动重试）。

### 4.3 新 API
- `POST /v1/projects/<id>/import-from-archive {gid, token}` → 200 + task 摘要（创建项目时自动触发，或项目页手动重试）
- `GET  /v1/projects/<id>/import-task` → 任务状态（前端轮询；任务已关闭提示时前端不渲染）
- `POST /v1/projects/<id>/import-task/dismiss` → 永久关闭该项目所有非运行中导入任务的提示（ACCESS 权限，返回 `{message, dismissed: true}`；运行中的任务不受影响）
- 路由注册在 `app/apis/urls.py` 的 project blueprint。

### 4.4 归档下载任务（新 `app/tasks/archive_import.py`）
```
1. 校验档案 API 基址（团队 `archive_api_url` → 全局 `ARCHIVE_PROVIDER_API_URL`）与团队 key，缺失则 FAILED（error 分别为「系统未配置档案 API 地址」/「团队未配置档案 API key，请联系管理员」）
2. archive_provider.resolve(gid, token, api_key) → ArchiveInfo
   → 成功返回 Hath 归档直链（*.hath.network/archive/...，canonicalize 后）
   → 失败重试（仿 eh-tools archive_resolvers：key 换序重试）
3. 流式下载 zip → 临时目录（requests，限大小上限 ARCHIVE_MAX_ZIP_BYTES、限时）
   - 仅允许 https；host 白名单校验（*.hath.network 等，防 SSRF）
4. 完整性校验（移植 eh-tools validate_archive_file/archiver.py 分层逻辑）：
   - 基础层（默认必做）：zipfile 逐条目流式读取 + CRC 校验 → 能正常解压、无损坏
   - 强校验（可选加强，仅当 API 响应提供时启用）：
     a. expected_content_bytes → 解压后总字节数比对
     b. expected_hashes（SHA-1 清单）→ 逐文件哈希比对 + 数量一致
   - 校验失败分类：INVALID_ARCHIVE（zip 损坏）/ CONTENT_MISMATCH（内容不对）
5. 解包：仅导入图片文件（jpg/jpeg/png/bmp/gif/webp）：
   - zip 内平铺根目录 → 全部图片直接导入（第三方归档形态）
   - zip 内 images/ 结构 → 剥掉 images/ 前缀（现有导入约定）
   - 跳过非图片/隐藏文件/目录项；页序按自然排序
   - 逐条目大小/数量上限（防 zip 炸弹）
6. 逐个 project.upload（复用现有上传/缩略图/OSS/去重逻辑）
7. 更新任务状态与进度；失败记录 error 供前端展示
8. run_sync 回退（仿 output_project 的 _FORCE_SYNC_TASK，测试用）
```

### 4.5 档案 API 适配器（从 eh-tools `archive_resolvers.py` 移植，契约已确认）
- **v1 只支持 NEW_API**（用户已确认）；LEGACY_API/SITE 不实现。
- 定义接口 `ArchiveProvider`（可插拔，测试可用桩替换）：
  ```python
  @dataclass
  class ArchiveInfo:
      zip_url: str            # 规范化的 *.hath.network/archive/<gid>/<key_a>/<key_b>/2?start=1
      title: str | None = None
      total_pages: int | None = None
      zip_size: int | None = None
      expected_hashes: tuple[str, ...] | None = None   # 强校验 B 可选

  class ArchiveProvider(Protocol):
      def resolve(self, gid: str, token: str, api_key: str) -> ArchiveInfo: ...
  ```
- **NEW_API 契约**（移植 `_resolve_new_api`）：
  ```
  POST  {api_url}/api/v1/parse
  Headers: Authorization: Bearer <key>, Content-Type: application/json
  Body:   {"gallery_id": "<gid>", "gallery_key": "<token>", "force": false}
  OK:     响应 {archive_url, gp_cost} 且无 "error" 字段
  ```
- **多 key**：团队配置的 `archive_api_keys` 列表，任务按序尝试，首个成功者生效（与 §4.1 一致）。
- 直链规范化移植 `canonicalize_archive_url`（hath.network host + key_a/key_b 提取）。
- 配置：`app/config.py` 加 `ARCHIVE_PROVIDER_API_URL`（默认全局；团队可覆盖）、`ARCHIVE_MAX_ZIP_BYTES`。（注：无 `ARCHIVE_PROVIDER` 导入路径配置，适配器按 NEW_API 契约固定实现，见 §8。）

## 5. 安全与边界

| 风险 | 对策 |
| --- | --- |
| API key 泄露 | 团队设置仅管理员可见；禁止日志输出；不在前端回显完整 key |
| zip 炸弹 | 下载大小上限（ARCHIVE_MAX_ZIP_BYTES）；解包时逐条目大小/数量上限 |
| SSRF | 直链仅 https；host 校验：`*.hath.network` 白名单（第三方 API 返回的直链经 canonicalize 后必然是该域名）；不跟随任意重定向 |
| 非法文件名 | 走现有 `Filename._check_valid`（拒绝 \ / : 等字符），失败条目跳过并计数 |
| 并发/重复 | 每项目单任务去重（运行中任务存在则拒绝触发；已完成/失败可再次导入） |
| 权限 | 沿用现有项目/文件权限体系（ADD_FILE 校验复用 project.upload 路径） |

## 6. 测试与部署

- 单元/接口测试（`tests/tasks/test_archive_import.py` 16 例 + `tests/api/test_archive_import_api.py` 18 例 = 34 例全绿，含 dismiss 关闭/运行中不受影响/权限拒绝 3 例）：
  - URL → gid/token 解析（前端正则 + 后端冗余校验）
  - 假 ArchiveProvider（内存 zip 直链）跑通：创建项目 → 触发任务 → 下载 → 校验 → 导入 → 项目文件数正确
  - 失败路径：无 key / API 超时 / zip 损坏 / 超大小 / 无图片条目
  - run_sync 同步执行（TESTING=YES 约定沿用）
- 部署：走 `moeflow.ps1 -Action Deploy -AllowDirty`；低内存开发服务器先关闭旧应用容器，部署后按分组方式运行回归。

当前开发服务器的归档导入专项已包含在后端低内存全量回归中。专项测试共 34 例，
覆盖 URL 解析、假 provider、归档下载和 CRC 校验、图片导入、多 key、失败状态、权限、
运行中去重、脱敏和 dismiss。完整回归结果为 `475 passed`（4 个 subtests），详见
[development-status.md](development-status.md)。

## 7. 已确认的实现决策

1. ~~第三方档案 API 的请求/响应契约~~ → **已确认**：只实现 NEW_API（`/api/v1/parse`，Bearer key，契约见 §4.5）；LEGACY_API/源站不做。
2. ~~创建即触发 vs 项目页手动触发~~ → **已实施（两者都做）**：创建表单填画廊 URL → 创建成功后前端二次调用 `POST import-from-archive`；失败后项目页提供「重试」按钮再次调用同一接口。
3. ~~zip 内是否带 `project.json`/`translations.txt`~~ → **已实施**：按纯图片处理，只导入图片（`images/` 前缀剥除 + 嵌套取 basename）。
4. ~~完整性校验强度~~ → **已实施**：默认 CRC 逐条目解压校验（`_stream_test_zip`）；哈希清单比对仅在 API 响应提供时启用（第三方 API 目前不带哈希，接口留口子）。
5. ~~API key 数量~~ → **已确认并实施**：团队级多 key（列表，按序尝试首个成功；前端团队设置增删，仅管理员）。

## 8. 实际实现对照（与设计稿的差异）

- 模型字段（`app/models/archive_import.py`）：`project/user/gid/token/gallery_url/status/stage/total_pages/completed_pages/error/zip_url/dismissed/create_time/update_time`，db_field 缩写 `p/u/g/t/gu/s/st/n/c/e/zu/d/ct/ut`；`set_progress(status, stage, total, completed, error, zip_url)` 单次原子更新；`latest(project)`/`to_api`（输出 `dismissed`，老文档缺失字段安全兜底为 false）/`clear`（当前任务流程未调用；后续导入放行由 API 层 RUNNING 去重控制）。
- 路由（`app/apis/urls.py` project blueprint）：
  - `POST /v1/projects/<id>/import-from-archive`（body `{gid, token, gallery_url?}`；WORKING + ADD_FILE + RUNNING 去重）
  - `GET  /v1/projects/<id>/import-task`（ACCESS 权限 → `latest`，无任务返回 `{"task": null}`）
  - `POST /v1/projects/<id>/import-task/dismiss`（ACCESS 权限；`status__nin=RUNNING` 批量置 `dismissed=true`）
- 任务（`app/tasks/archive_import.py`）：`resolve_archive_url`（NEW_API）→ `_download_zip`（流式 + https + `ARCHIVE_MAX_ZIP_BYTES` 超限 ValueError）→ `_stream_test_zip`（CRC）→ `_zip_image_entries`（剥 `images/`、嵌套取 basename、跳隐藏/非图片、`ARCHIVE_MAX_ZIP_ENTRIES`）→ 逐个 `project.upload`；finally `rmtree` 临时目录；`import_archive_from_gallery(project_id, gid, token, run_sync)` 触发（TESTING 同步执行）。
- 配置（`app/config.py`）：`ARCHIVE_PROVIDER_API_URL`（全局默认）、`ARCHIVE_MAX_ZIP_BYTES`（默认 500MB）、`ARCHIVE_MAX_ZIP_ENTRIES`（默认 5000）、`ARCHIVE_MAX_ENTRY_BYTES`（默认 128MB）。
- 团队 key（`app/models/team.py` + `app/apis/team.py` + `EditTeamSchema`）：字段 `archive_api_keys = ListField(DictField(db_field="aak"))`，每项 `{id(uuid4), key, remark, enabled}`；`PUT /v1/teams/<id>` 时 `_apply_archive_api_keys` 按 id 增/改/删；`to_api` 返回脱敏 `{id, remark, enabled, key_tail}`，明文永不出 API。另有 `archive_api_url`（db_field="aau"）团队级 API 基址（空=全局默认），任务内解析时 `team.archive_api_url or config["ARCHIVE_PROVIDER_API_URL"]`。
- 前端：
  - `ProjectCreateForm.tsx`：可选「画廊网址」输入框，`/g/(\d+)/([A-Za-z0-9]+)` 校验解析；创建成功 → `api.project.importFromArchive`（失败 message 提示不阻断）。
  - `ArchiveImportProgress.tsx`（新组件，挂载于 `ProjectFiles.tsx` FileList 上方）：3s 轮询 `GET import-task`；`status=5` 且此前为运行中 → `location.reload()` 刷新文件列表；`status=6` → 错误 + 重试按钮（回填 task 的 gid/token 重新触发）；任务 `dismissed=true` 时视为无任务不渲染；成功/失败横幅均提供「不再提示」按钮（调 dismiss 接口后立即隐藏）。
  - `TeamSettingBase.tsx`：管理员可见的「画廊归档 API key」区：显示 `key_tail` 列表（删除）+ 输入框（回车/按钮添加），整体提交 `archiveApiKeys`。
  - i18n：`messages.yaml` 新增 `project.archiveUrl*` / `project.archiveImport*`（含 `archiveImportDismiss` / `archiveImportDismissFailed`）/ `site.archiveApiKey*`（zh-cn.json / en.json 由 `scripts/generate-locale-json.ts` 生成）。
- 测试：`tests/tasks/test_archive_import.py`（单元 + 全流程桩 zip 导入 + 多 key 顺序）+ `tests/api/test_archive_import_api.py`（触发/查询/权限/去重/团队 key 管理/脱敏/dismiss 关闭）共 34 例全绿。

# Moeflow 开发环境与当前开发状态说明

更新时间：2026-08-27
工作区：`D:\moeflow`

## 1. 当前结论

当前工作区已经具备前端开发和后端开发所需的基础依赖：

- 前端依赖完整，`typecheck` 和生产构建均通过。
- 后端使用系统 Python 3.12.5，并按升级后的 `requirements.txt` 安装依赖，不再把虚拟环境作为运行入口。
- 旧的 Flask-APIKit 已移除；项目实际使用的 API 响应、异常、分页和查询功能已迁移到 `app.core.api`，没有再引入另一个 API 框架。
- 最近一次开发服务器验收使用 release `moeflow-20260827-214208-1b8c76e1-3282a7f5`；后端全量测试为 `475 passed`（含 4 个 subtests）。
- 生产归档仅用于开发服务器迁移验证，生产服务器本身未被修改。
- 前后端都有大量未提交修改，当前处于功能开发中的脏工作区状态，部署前需要先整理和验证。

## 2. 环境与依赖状态

### 基础工具

| 组件 | 状态 | 说明 |
| --- | --- | --- |
| Node.js | 已安装，v24.18.0 | 满足前端构建需求 |
| npm | 已安装，v11.16.0 | 前端依赖管理可用 |
| Python 3.12 | 3.12.5 | 后端运行版本 |
| 后端依赖 | 系统 Python 环境 | 通过 `py -3.12 -m pip` 管理 |
| git / tar / ssh / scp | 已安装 | `moeflow.ps1` 所需命令完整 |
| Docker | 本机未检测到 | 本地不用于当前测试流程 |

### 前端 `moeflow-frontend`

- `node_modules` 已存在。
- 执行过 `npm install --ignore-scripts`，使安装结果与 `package-lock.json` 对齐。
- `npm ls --depth=0` 无缺失包。
- 验证结果：
  - `npm run typecheck` 通过。
  - `npm run build` 通过。
- 注意：`npm audit` 报告若干已知漏洞。该问题不影响本次开发环境可用性，但后续应安排依赖升级或风险评估。

常用命令：

```powershell
cd D:\moeflow\moeflow-frontend
npm install --ignore-scripts
npm start
npm run typecheck
npm run build
```

### 后端 `moeflow-backend`

后端直接使用系统 Python 3.12.5：

```powershell
cd D:\moeflow\moeflow-backend
py -3.12 --version
py -3.12 -m pip install -r requirements.txt
```

依赖校验结果：

| 项目 | 结果 |
| --- | --- |
| `requirements.txt` 锁定包数量 | 69 个唯一运行包 |
| 缺失包 | 0 |
| 版本不一致 | 0 |
| 关键模块导入 | 通过 |
| `compileall app tests manage.py` | 通过 |

关键依赖均已可导入，包括但不限于：

- Flask / flask-babel / app.core.api
- Celery / Flower
- boto3 / botocore
- Google Cloud Storage
- MongoEngine / PyMongo
- OSS2
- Pillow
- Redis

开发测试依赖位于 `requirements-dev.txt`；本地验证使用 Python 3.12 系统环境执行。

依赖文件职责和本次升级如下：

| 范围 | 当前版本/方式 | 变更说明 |
| --- | --- | --- |
| 直接依赖声明 | `requirements.in` | 只维护应用直接依赖，不手写传递依赖 |
| 运行时锁文件 | `requirements.txt` | 由 `uv pip compile` 按 Python 3.12 生成，当前 69 个唯一包 |
| 开发/测试锁文件 | `requirements-dev.txt` | 在运行时锁文件上加入 pytest、Ruff、mongomock 等工具 |
| Web/API | Flask 3.1.3、Marshmallow 3.26.2 | Flask-APIKit 已删除，APIKit 的本地功能由 `app.core.api` 承担 |
| MongoDB | MongoEngine 0.29.3、PyMongo 4.17.0 | 从旧 MongoEngine/PyMongo 组合升级到 PyMongo 4 |
| 存储与图像 | GCS 3.13.1、Boto3 1.43.80、OSS2 2.19.1、Pillow 12.3.0 | 更新云存储 SDK 和图像处理库 |
| 任务与服务 | Celery 5.6.3、Flower 2.1.0、Gunicorn 23.0.0 | 与 Python 3.12 运行基线同步 |

重新生成锁文件：

```powershell
cd D:\moeflow\moeflow-backend
py -3.12 -m pip install -r requirements-dev.txt
uv pip compile requirements.in --python-version 3.12 -o requirements.txt
uv pip compile requirements-dev.in --python-version 3.12 -o requirements-dev.txt
```

## 3. 本地运行注意事项

后端本地完整运行仍需要配置和服务支持：

- 需要基于 `moeflow-backend\.env.sample` 或项目内配置流程创建实际开发配置。
- 需要 MongoDB、RabbitMQ 等外部服务。
- 当前机器未检测到本地 MongoDB、RabbitMQ、Redis 服务命令。
- 因此本地只做了依赖导入、编译和应用加载冒烟检查，未启动完整后端。

网络代理说明：

- 可使用本地代理端口 `127.0.0.1:2080`。
- 本次安装 Python 依赖时，`pypi.org` 经代理访问存在 TLS 握手问题，改用阿里云 PyPI 镜像完成安装。
- 该代理配置只在当次命令会话中使用，未写入全局配置。

示例（仅使用系统 Python，不创建虚拟环境）：

```powershell
$env:HTTP_PROXY  = 'http://127.0.0.1:2080'
$env:HTTPS_PROXY = 'http://127.0.0.1:2080'
py -3.12 -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

`dsh web` 不是 MoeFlow 的后端或前端运行依赖。前端开发服务器请在
`moeflow-frontend` 中执行 `npm start`；后端可执行 `py -3.12 manage.py run`，或使用
`moeflow-deploy/Makefile` 中的 `backend-dev`。如果终端提示找不到 `dsh`，先用
`Get-Command dsh` 检查 PATH；本机还可以用 Everything CLI 搜索 `dsh.cmd`。

## 4. 测试与部署脚本

统一入口是根目录的 `moeflow.ps1`。

生成部署计划：

```powershell
cd D:\moeflow
.\moeflow.ps1 -Action Plan -AllowDirty
```

远端测试：

```powershell
.\moeflow.ps1 -Action Test -KeyPath <有效的SSH私钥路径>
```

注意：

- 脚本默认 SSH 私钥路径指向 `G:\Temp\MobaXterm\.ssh\m_usag`，当前不可用。
- 如需执行远端测试，必须显式传入有效 `-KeyPath`。
- 私钥不应提交到仓库，建议保存在工作区外的安全位置并控制权限。
- 当前前后端均有大量未提交修改，`Plan` 不加 `-AllowDirty` 会拒绝执行；这是预期保护机制。

## 5. 当前开发情况总结

### 开发服务器当前状态（2026-08-27）

| 项目 | 当前值 |
| --- | --- |
| Tailscale 地址 | `100.90.141.40` |
| SSH 端口 | `23213` |
| 应用入口 | `http://100.90.141.40:5080/` |
| 部署目录 | `/root/moeflow-deploy-20251003` |
| release 根目录 | `/root/moeflow-releases` |
| 当前 release | `moeflow-20260827-214208-1b8c76e1-3282a7f5` |
| 主机内存 / swap | 约 960 MiB / 1 GiB |
| 后端与两个 worker 限制 | 每个容器 512 MiB |

当前后端、两个 Celery worker、前端、MongoDB 和 RabbitMQ 容器均正常运行。
开发服务器是低内存验收环境，部署或测试时必须遵守以下顺序：

1. 部署新 release 前先关闭旧的后端、worker 和前端应用容器；MongoDB、RabbitMQ
   可保留运行，避免同时占用两套应用进程的内存。
2. 完整 pytest 不开启 coverage，不使用 `pytest-xdist` 并发；按 `tests/base`、
   `tests/api`、`tests/model`、`tests/other` + `tests/tasks` 分组串行执行。
3. 每组结束后释放测试进程和临时容器，并用 `docker stats` 观察内存；不要在这台
   服务器上重复一次性全量 coverage 测试。

本次低内存全量验收使用：

```text
pytest -q -o addopts= --no-cov -p no:cacheprovider --disable-warnings --maxfail=1
```

各组结果为：`tests/base` 51 passed（4 subtests）、`tests/api` 210 passed、
`tests/model` 179 passed（4 subtests）、`tests/other` + `tests/tasks` 35 passed，
合计 `475 passed`（4 subtests）。

迁移验证使用开发服务器上的生产归档副本：

```text
/root/moeflow-prod-20260816-104619.archive.gz
```

该 `mongodump --archive --gzip` 归档恢复约 680,879 条文档。隔离库和开发主库均已
验证 `0000` 至 `0006` 顺序迁移成功，`0004` 真实数据迁移约 50 秒；测试结束后已清理
隔离库，原始归档仍保留在服务器。按授权，开发数据库可以直接清空后重测，不需要为
此操作保留旧数据库备份；这条规则不适用于生产服务器。

### 分支状态

前后端当前位于同名开发分支：

```text
thumbnail-async-and-migrations
```

当前 HEAD：

| 仓库 | 分支 | HEAD |
| --- | --- | --- |
| `moeflow-frontend` | `thumbnail-async-and-migrations` | `1b8c76e` |
| `moeflow-backend` | `thumbnail-async-and-migrations` | `3282a7f` |

Git 工作区状态：

| 仓库 | 变更条目数量 | 状态 |
| --- | ---: | --- |
| `moeflow-frontend` | 105 | 有大量修改和新增文件 |
| `moeflow-backend` | 77 | 有大量修改和新增文件 |

这些变更尚未提交或整理，属于进行中的功能开发状态。

### 前端开发内容概况

从当前修改范围看，前端主要涉及以下方向：

- API 层大范围调整，包括认证、文件、项目、团队、成员、邀请、站点设置、翻译输出等接口封装。
- 项目成员、团队成员相关展示和排序能力增强。
- 新增身份标签 / 身份标签策略相关工具与测试。
- 新增成员统计、项目成员、邀请选项等工具函数和测试。
- 新增归档导入进度组件 `ArchiveImportProgress`。
- 多个项目、团队、标记、翻译、输出页面和 Redux store 有配套修改。
- 删除了旧的 `AdminImageSafeCheck` 组件。

### 后端开发内容概况

后端当前改动集中在数据模型、权限关系和归档导入能力：

- 引入或完善项目成员、团队成员、身份标签、身份操作、审计等模型。
- 新增身份权限、项目生命周期、邀请、用户别名等服务层逻辑。
- 调整认证、用户、项目、团队、邀请等 API。
- 删除旧的 `app/apis/member.py` 和 `app/apis/project_workers.py`。
- 新增归档导入相关模型、API、校验器和任务。
- 新增迁移：
  - `m0004_identity_members.py`
  - `m0005_identity_member_index_options.py`
  - `m0006_search_projections.py`
- 补充了归档导入、身份模型、身份权限、成员服务、导出等相关测试。

**近两日（2026-08-25 至 08-26）新增的性能优化（见 `moeflow-performance-progress.md` 与 `identity-refactor-review-and-fixes.md` §15）：**

- 项目列表 / 项目集 / 团队列表接口改为「列表卡片序列化器 + 批量快照预取」，列表接口不再执行详情页级序列化（TTFB ~546ms → ~36ms，payload 159KB → 16.2KB）。
- 团队成员列表 N+1 优化：`TeamMember.to_api(user_map=...)` 批量上下文 + `TeamMemberListAPI` 批量预取 user 并只序列化页切片 + `list_members` 排序批量预取。
- 前端快捷编辑请求合并：项目成员 active+invited 单请求，点击「人员快捷编辑」从 3 次降为 2 次。
- 清理 5 个未使用 import，`ruff check app/` 全绿。
- 已多次部署开发服务器；当前 release 为 `moeflow-20260827-214208-1b8c76e1-3282a7f5`，
  容器内低内存分批全量测试通过。

### 最近提交方向

后端最近提交显示本分支此前已完成：

- 数据库迁移框架。
- 缩略图异步生成。
- 缩略图生命周期状态。
- 文件和 OSS 路径加固。

前端最近提交主要包含：

- 空搜索词处理优化。
- 符号输入器折叠逻辑增强。
- 深色模式主题颜色增强。

## 6. 已知问题与风险

1. **工作区较脏**
   - 前后端共有大量未提交修改。
   - 不建议在未整理前直接执行部署。
2. **迁移需要按生产发布流程验证**
   - 当前迁移链为 `0000` 至 `0006`，真实生产归档已在开发服务器完成重放验证。
   - `0004`、`0005` 属于不可逆迁移；生产执行前仍需独立完成快照、恢复和发布评审。
3. **Lint 现状**
   - `ruff check app/` 已全绿：原先报出的 5 个未使用 import（`app/apis/archive_import.py`、`app/services/project_invitation.py`、`app/tasks/archive_import.py`×2、`app/validators/project.py`）已清理。
4. **本地完整运行条件不足**
   - 缺少实际 `.env` 和本地中间件服务。
   - 目前只能进行静态、导入和构建级验证。
5. **前端存在依赖审计告警**
   - `npm audit` 报告若干漏洞。
   - 后续应评估是否需要升级依赖或引入 override。
6. **远程测试私钥需显式确认**
   - 脚本默认 key 路径不保证在每台 Windows 机器上存在。
   - 执行 `moeflow.ps1 -Action Test` 或部署前需要显式提供有效私钥，并确保 Tailscale 节点已连接。
7. **开发服务器内存限制（2026-08-27 记录）**
   - 开发服务器约 960 MiB 内存、1 GiB swap，后端和两个 worker 容器各上限 512 MiB。
   - 容器内跑完整测试须**分批 + `--no-cov` 禁用 coverage**，一次性全量 coverage 测试会 OOM 导致服务器重启。
   - `moeflow.ps1 -Action Deploy` 在后台非交互下会被 `ShouldProcess` 拦截，需 `$ConfirmPreference='None'` 显式放行。

## 7. 建议下一步

1. 整理前后端未提交修改，按功能拆分为可审查的提交。
2. 使用有效 SSH key 通过 `moeflow.ps1 -Action Verify` 检查部署；`-Action Test` 仅覆盖无应用数据写入的安全验收门。
3. 新增身份成员模型、索引选项和 `0000` 至 `0006` 迁移已在开发服务器验证通过；后续生产发布仍需独立评审。
4. ✅ 清理当前 `ruff check` 报出的未使用 import——已完成，`ruff check app/` 全绿。
5. 处理或记录前端 `npm audit` 风险。
6. 若 `package-lock.json` 的自动校准变更符合预期，应随依赖整理一起提交。
7. 部署/测试注意（2026-08-27 记录）：开发服务器低内存，容器内跑完整测试须分批 + `--no-cov`；后台非交互部署需 `$ConfirmPreference='None'`。
8. 部署前先重新执行：
   ```powershell
   npm run typecheck
   npm run build
   py -3.12 -m compileall app tests manage.py
   ```

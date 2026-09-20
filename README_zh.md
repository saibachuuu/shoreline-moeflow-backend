# 萌翻[MoeFlow]后端项目

由于此版本调整了部分 API 接口， **请配合萌翻前端 Version.1.0.1 版本使用！** 直接使用旧版可能在修改（创建）团队和项目时报错。

此版本需配置 **阿里云 OSS** 作为文件存储。如果需要使用其他文件存储方式，可以选择使用以下的分支版本：

- **本地硬盘存储** [`scanlation/moetran-local-backend`](https://github.com/scanlation/moetran-local-backend)

## 安装步骤

1. 安装 Python 3.10 版本
2. 依赖环境 MangoDB、Erlang、RabbitMQ
3. `pip install -r requirements.txt` （这一步如果 Windows 有报错，请在环境变量里面加 `PYTHONUTF8=1` ）
4. 以 `/config.py` 为模板创建 `/configs/dev.py` 用于开发（此目录已被 git ignore）
5. 开发时，请直接在 `/configs/dev.py` 文件里面修改必填的配置
6. 运行前注意配置环境变量 `CONFIG_PATH=../configs/dev.py`
7. 运行主进程： `python manage.py run`
8. 在 `DEBUG` 开启的情况下，注册等验证码信息，直接看命令行输出的日志信息。
9. _(可选)_ 导入、导出等功能需要依赖两个 celery worker 进程，调试时可按另附的步骤启动。

## 配置 Celery

1. 如果使用 Windows 跑 Celery Worker，需要先安装 `eventlet` 并修改参数，否则会提示： `not enough values to unpack (expected 3, got 0)`
2. _(可选)_ Windows 安装 `eventlet` 请执行： `pip install eventlet`
3. 两个 worker 需要启动两个命令行（**这里的方案使用 Windows 的 Powershell 举例**），运行前需配置环境变量：`CONFIG_PATH=../configs/dev.py`
4. 启动主要 Celery Worker (发送邮件、分析术语)，请执行：`celery -A app.celery worker -n default -P eventlet --loglevel=info`
5. 启动输出用 Celery Worker (导入项目、生成缩略图、导出翻译、导出项目)，请执行：`celery -A app.celery worker -Q output -n output -P eventlet --loglevel=info`
6. 非 Windows 环境如果有报错，请去掉命令中的 `-P eventlet` 一段。

## 如何测试

1. 配置测试 `test.py`
   1. DEBUG = True 和 TESTING = True
   2. DB_URI 协议名使用 `mongomock://` 并将数据库名以 `_test` 结尾
   3. 将 `CONFIRM_EMAIL_WAIT_SECONDS` `RESET_EMAIL_WAIT_SECONDS` `RESET_PASSWORD_WAIT_SECONDS` 设置为 `1`，以免过多等待
2. 执行 `export CONFIG_PATH=/path/to/configs/test.py && pytest -n auto` 开始并行测试

## 版本更新内容

### Version 1.2.0 (Shoreline 欶澜定制版)

#### 核心与前后端协同功能
- **单独压缩预览图片**：重采样大图与小尺寸缩略图独立异步生成，优化存储与并发处理，显著改善图片加载与传输性能。
- **项目卡片快捷登记人员**：支持在项目列表卡片直接快速登记与变更工作人员名单，配合前端实现即时人员分工同步。
- **用户别名与身份体系**：支持用户个性化全局别名维护、团队创建者自定义覆盖以及显示名称首选项配置。
- **轮询更新项目列表编辑状态和最新数据**：引入 Presence 协作心跳机制与动态活跃状态接口，支持毫秒级感知他人编辑状态并广播列表变更。
- **按成员/跨项目集搜索项目**：支持跨团队、跨项目集按具体成员与限定角色（初翻、嵌字、校对、生肉等）进行组合筛选。
- **邮件寄送校对稿**：支持向译者寄送包含排版优化的图文 Diff 变更对照、页面左侧内嵌缩略图、多收件人抄送的专业校对反馈邮件（严格适配 RFC 2046 规范）。
- **从 TG Bot 导入漫画**：提供异步归档压缩包导入接口（Archive Import），支持对接 Telegram Bot 等外部机器人自动解包并一键建立汉化项目。

#### 后端独占特性与服务支撑
- **导出时自动在指定页面插入成员名单**：导出完整漫画作品时，支持根据项目登记人员自动排版生成工作人员名单页，并插入至指定位置（如首页、第 2 页或末页）。
- **为外部提供站内项目查询**：开放防撞车与进度同步查询接口（合作团队项目碰撞搜索 API），支持合作组与外部机器人安全检索开坑与进度状态。
- **Cloudflare R2 / S3 兼容对象存储支持**：原生集成 `boto3` / `botocore`，完善针对 Cloudflare R2 及 S3 协议图床的无缝上传、签名与流式传输支持。
- **版本化数据库迁移架构**：内置基于 `manage.py migrate` 的自动化版本迁移机制，保障数据库结构升级无损平滑。
- **轻量级性能与索引优化**：重构大表复合查询索引与投影查询，大幅削减大批量漫画项目列表加载延迟。

### Version 1.2.1

- **成员变更管理员标签修复**：允许项目管理员与团队管理员在成员变更操作中指派管理员标签（admin tag）。

### Version 1.2.2

- **直接自荐**：将自己加入项目职位时，直接激活成员记录，不再生成自我邀请。
- **开放资格模式招募**：开放资格模式下，普通成员可为他人指派工作人员标签并更新标签，不再被管理员权限门槛拦截。
- **缺失标签导致的 500 修复**：新增 `_LegacyTagsField` 与 m0007 迁移，将缺失/null 的 tags 视作空列表，防止带职位成员打开项目集时抛出 TypeError 500 错误。

### Version 1.2.3

- **绑定外部别名时自动合并**：`bind()` 在目标注册用户已存在项目成员记录时，自动将外部别名合并进该记录（职位标签取并集、采用外部显示名、硬删除外部记录），不再抛出 `MEMBER_MERGE_REQUIRED`。
- **已移除外部成员可绑定**：允许处于软删除（removed）状态的外部成员绑定注册用户，并将其复活为生效的注册成员。
- **外部别名硬删除**：新增 `hard_delete_external()`，项目创建者或团队创建者可永久删除外部别名并释放 `(project, external_id)` 唯一槽位；新增 `DELETE /v1/projects/<id>/members/<member_id>` 接口。
- **错误码增强**：`APIError.to_tuple()` 在数字错误码之外附带稳定的 `identity_code` 字符串，便于前端在并发合并等场景精确兑底。
- **环境与 CI 修复**：恢复 Python 3.12 运行环境（与数据库迁移校验和一致）并同步 uv 生成的依赖；`user.py` 改用 `jwt.decode` 替换已被移除的 `TimedJSONWebSignatureSerializer`；修正 marshmallow 3.26 下 `ValidationError(msg, [field_name])` 触发的 unhashable type 错误；CI 改为安装 `requirements-dev.txt` 以便运行 pytest，并整理 ruff 格式。
- **测试**：将 bind 用例更新为预期自动合并，新增 owner 合并、硬删除、已移除外部成员绑定等用例，并将迁移 0007 纳入预期迁移列表。

# 可选模块系统（外挂模块）设计与调查报告

> 状态：**设计稿，未实施**。
> 目标：为 Shoreline 定制版提供一套「目录即开关」的外挂模块机制，
> 使定制功能（跨组检索、归档导入等）与上游 Moeflow 核心解耦。
> 本文档同时覆盖后端（`moeflow-backend`）与前端（`moeflow-frontend`）。

---

## 1. 目标与非目标

### 1.1 两条硬性约束（需求方确认）

| 编号 | 约束 | 含义 |
| --- | --- | --- |
| **C1** | **不带模块时，核心必须保持通用** | 核心代码不得出现任何具体模块的名字、字段、开关；删掉 `app/modules/` 全部内容后，项目仍能构建、启动、通过测试，且行为与从未引入模块系统时一致。 |
| **C2** | **模块不承诺向后兼容与数据迁移** | 模块可以随意调整自己的数据结构，不写迁移、不兼容旧数据。模块自有数据必须可重建。 |
| **C3** | **改法尽量精简且无潜在问题** | 能在模块内解决的绝不改核心；能用既有模式的不引入新机制；宁可少功能也不要留隐患。 |

> C2 是这套设计能成立的关键：它让模块**完全绕开**核心迁移链
> （`app/migrations/runner.py` 是全局线性、AST 校验和、租约锁且拒绝乱序的强约束系统，
> 见 §3.4）。模块自带集合、自建索引、失效即重建，核心迁移器一行都不用改。

### 1.2 明确的非目标

- **不做通用插件框架**。不定义公开契约，不支持第三方作者，不做运行时装卸。
- **不做前端开关**。模块启用与否不由站点设置/前端界面控制。
  （本文档按「开关只体现为模块目录/配置文件是否存在，不新增任何前端管理界面」理解；
  若需求方所指为其他含义，§4.3 的机制可相应调整。）
- **不追求模块间互相依赖**。模块之间不建立依赖关系。
- **本期不做全库撞车普查**。§6 的模块是「新建项目时的查重护栏」，不是普查工具（见 §6.2.2）。

### 1.3 为什么是「模块」而不是「插件」

| | 插件框架（扩展性） | 外挂模块（隔离） |
| --- | --- | --- |
| 目的 | 让别人不改你代码就能加功能 | 让自己的定制代码不弄脏核心 |
| 成本 | 高：公开契约、生命周期、沙箱 | 低：一个目录 + 一次扫描 |
| 门槛 | 第三方作者 + 插件数 > 5 | 立刻见效 |

当前的痛点是与上游合并（`dev` 领先 `upstream/main` 45 个提交），属于**隔离**问题。
框架化的成本（重构 app factory、改造迁移系统）买不到这里需要的东西。

---

## 2. 现状调查

### 2.1 引导顺序（决定注册表形态的关键）

`app/__init__.py` 在 **import 期**就构建了全部单例：

```python
flask_app = create_flask_app(Flask(__name__, ...))   # 1
configure_extra_logs(flask_app)
celery = create_celery(flask_app)                    # 2 ← 需要 task 包列表
init_flask_app(flask_app)                            # 3 ← register_apis / init_api / oss.init
```

要点：

- **没有 `create_app(config)`**。`create_flask_app` 还带
  `assert not _create_flask_app_called`，明确只允许调用一次。
- **Celery 早于 `init_flask_app`**（第 2 步 vs 第 3 步）。因此模块注册表必须能被
  两个不同的接线点分别读取 → **注册表只能是无副作用的 import 期元数据**，
  不能是「启动时构建、带副作用」的对象。这是 §4.3 设计的直接原因。

可用的接线点：

| 位置 | 文件 | 作用 |
| --- | --- | --- |
| `create_celery` | `app/factory.py` | `autodiscover_tasks(packages=[...])`（当前硬编码列表）、`task_routes` |
| `init_flask_app` | `app/factory.py` | `register_apis(app)` / `init_api(app)` / `oss.init(...)` |

蓝图注册机制（`app/apis/__init__.py`）：`register_apis(app)` 扫描 `vars(urls)` 中所有
`Blueprint` 实例并注册。即蓝图的发现依赖它出现在 `app/apis/urls.py` 的模块命名空间里。

### 2.2 已经存在的两个「事实模块」

#### (a) MIT（manga-image-translator）——真正的跨仓库外挂

| 环节 | 位置 | 形态 |
| --- | --- | --- |
| 配置开关 | `app/config.py:182` | `MIT_STORAGE_ROOT = env.get("MIT_STORAGE_ROOT", None)` |
| 蓝图门控 | `app/apis/urls.py:612` | `if app_config["MIT_STORAGE_ROOT"]:` 后才定义 `mit` 蓝图 |
| 任务包注册 | `app/factory.py` | `autodiscover_tasks(packages=[..., "app.tasks.mit"])`，注释：*"its impl is in other repo"* |
| 独立队列 | `app/factory.py` | `("tasks.mit.*", {"queue": "mit"})` |
| 桩实现 | `app/tasks/mit.py` | 全部 `pass  # Real implementation is in manga_translator/moeflow_worker.py` |
| CLI | `manage.py:121-149` | `mit_file` / `mit_preprocess_dir` |

结论：**MIT 就是一套已经跑在生产里的非正式外挂模块**。本设计本质上是把它的做法形式化。
粗糙之处也正是要修掉的：开关逻辑内联在 `urls.py`、任务包硬编码在 `factory.py`。

#### (b) 归档导入——完整的垂直切片，但侵入核心

`app/apis/archive_import.py`、`app/models/archive_import.py`、
`app/tasks/archive_import.py`、`app/constants/archive_import.py`、
`app/validators/archive_import.py`、`docs/archive-import-from-gallery-url.md`
以及前后端测试，是**自成一体**的。这是很好的模块候选，
也是「异步任务 + 进度轮询」的参考实现（§6.3）。

### 2.3 侵入面盘点

| 核心文件 | 归档导入 | partner search（被查询） | MIT |
| --- | --- | --- | --- |
| `app/apis/urls.py` | 1 处 import（`:51`）+ 3 条路由挂在 project 蓝图内（`:433-446`） | 独立蓝图（`:601-610`） | 条件蓝图（`:612`） |
| `app/factory.py` | **未编辑（缺陷）**——见 §5.1 注 | — | 任务包 + 队列路由 |
| `app/config.py` | **7 项 `ARCHIVE_*`**（`:50-73`） | — | `MIT_STORAGE_ROOT` |
| `app/models/team.py` | **2 个持久化字段**（`:282-284`）+ `to_api`（`:558-559`）+ 新方法 `archive_api_keys_api`（`:592-609`） | — | — |
| `app/apis/team.py` | **import（`:35`）+ 55 行 helper `_apply_archive_api_keys`（`:58-112`）+ 核心 `TeamAPI.put` 分支（`:267-285`）** | — | — |
| `app/validators/team.py` | **2 个字段**（`:67-71`） | — | — |
| `app/utils/secrets.py` | **新增**，但内容是归档专属（`_fernet` 硬编码读 `ARCHIVE_API_KEY_ENCRYPTION_KEY`） | — | — |
| `app/models/site_setting.py` | — | **4 个字段**（`:31-39`）+ `to_dict`（`:70-73`） | — |
| `app/validators/site_setting.py` | — | **4 个校验字段**（`:32-39`） | — |
| `app/apis/site_setting.py` | — | **4 处赋值**（`:60-69`） | — |
| `.env.sample` / `.env.test.sample` | 各 1 块 | — | `MIT_STORAGE_ROOT` |
| 后端 `.po` 目录 | 有（约 20 条 msgid） | 有 | 有 |
| 前端 | 1 个新文件；改 5 个核心文件 + locales | **改 admin 页 + locales** | locales |

> **§2.3 注（重要）**：归档导入**并非**低侵入样本。它的**任务机制**（model/constants/task/validator/api）
> 完全私有、零注册文件，但为了「团队级 API key + 团队级基址」它侵入了 **核心 `Team` 文档**
> （2 个持久化字段）和**核心 `PUT /v1/teams/<id>` 处理器**（55 行 helper + 条件分支），
> 这是它最贵的一部分，也**正是 §6 模块应当完全避开的部分**——紫藤密钥放模块本地配置，
> 不进 `Team`，不进 `SiteSetting`。
>
> 另外 `app/utils/secrets.py` 是个**命名通用、内容专属**的陷阱：
> 名字看着是通用加密工具，实际 `_fernet()` 硬编码读 `ARCHIVE_API_KEY_ENCRYPTION_KEY`
> 且报错文案写死 "archive API keys"。模块若需要加密，应当自建，不要复用它。

可用的**低耦合有利事实**：

- `app/constants/__init__.py` 为空——常量模块无需注册，直接 import 即可。
- `app/validators/__init__.py` 只是便利桶（barrel）；
  `app/apis/archive_import.py` 直接从 `app.validators.archive_import` 导入，
  **不经过桶**。→ 模块的 validator 不需要注册。
- `app/models/__init__.py` 只有 `connect_db`，**没有模型注册表**。
  mongoengine 的模型靠 import 副作用注册 → 模块模型只需被自己的 api/tasks 导入即可。

### 2.4 迁移系统（模块必须绕开的原因）

`app/migrations/runner.py` 的强约束：

- `discover()` 用 `pkgutil` 扫描 `app.migrations.versions`，包路径写死。
- 对每个迁移的 `up()` 做 **AST 校验和**（`_checksum`），已应用的迁移被改动会直接
  `MigrationError: Applied migration ... has been modified`。
- `run_pending` **拒绝乱序应用**：
  `Cannot apply migrations out of order; these earlier migrations are still pending`。
- 带租约锁（`migration_lock`，`LOCK_LEASE = 30min`），`manage.py migrate` 是容器启动的
  `service_completed_successfully` 闸门。

把模块迁移塞进这条链会有两个后果：插件卸载后校验和残留会污染
`status()`；以及模块与核心的版本号相互阻塞。另起一套 runner 则要重新实现锁与幂等。

**结论：模块不进入核心迁移链（呼应 C2）。** 模块自建索引、失效即重建。

### 2.5 部署形态（对「自动化」是决定性约束）

来自 `scripts/moeflow-dev.ps1` 生成 compose override 的部分：

```yaml
moeflow-backend:      command: gunicorn -t 120 -w 1 -b 0.0.0.0:5000 "app:create_app()"
moeflow-celery-default: command: celery --app app.celery worker --queues default --hostname celery.default --logLevel=info --concurrency=1
moeflow-celery-output:  command: celery --app app.celery worker --queues output  --hostname celery.output  --logLevel=info --concurrency=1
```

关键事实：

1. **只有两个 Celery worker**（`default` / `output`），**均为 `--concurrency=1`**。
2. **全项目没有任何 Celery beat / 周期性调度**：全局搜索 `beat|periodic|crontab` 无业务命中，
   `create_celery` 里也没有 `beat_schedule`。
3. **Web 端 `gunicorn -w 1`**（单 worker）。
4. compose 文件**不在仓库里**，位于服务器 `/root/moeflow-deploy-20251003`，
   override 由 `scripts/moeflow-dev.ps1` 现场生成。
   → 新增容器/队列需要同时改**部署脚本**和**服务器 compose**。

> 因此「自动化查询」**不能依赖 Celery beat**。可选方案见 §6.5。

### 2.6 前端注册点

| 事项 | 现状 | 位置 |
| --- | --- | --- |
| 路由 | `App.tsx` 内扁平 `<Switch>`（`:614`）；`/dashboard` 渲染 `<Dashboard />` 再嵌套子路由 | `App.tsx:614-661`、`pages/Dashboard.tsx:297-336` |
| 条件路由先例 | `{userIsAdmin && <Route path={routes.admin}>}` | `App.tsx:653-657` |
| 路由常量 | `pages/routes.ts` 静态 `as const` 对象 | 全文 |
| 菜单 | 写死数组，含条件项（`currentUser.admin && {...}`）+ `.filter(Boolean)` | `DashboardMenu.tsx:448-514` |
| API 层 | `src/apis/index.ts` 是显式桶：逐个 import + default 合并导出 | `:16-30` / 末尾 |
| 构建工具 | **Vite 7.1.6**、React 17、react-router-dom 5.3.4 | `package.json` |
| i18n | `src/locales/messages.yaml` 是**唯一源**，`npm run build:locale` 生成 `zh-cn.json` / `en.json` | `scripts/generate-locale-json.ts` |
| 运行时配置 | `RuntimeConfig { baseURL }` + `lazyThenable` 读 `/moeflow-runtime-config.json`，注释 `// TODO: more fields can be added here` | `src/configs.tsx` |

重要可行事实：**Vite 7 支持 `import.meta.glob`**，前端也能做到「目录即开关」，无需维护注册表文件。

---

## 3. 设计

### 3.1 目录布局

```
moeflow-backend/
  app/modules/                 # 核心：通用扫描器 + 契约，不含任何模块名
    __init__.py                #   ModuleSpec / discover() / enabled_modules()
  app/modules/<name>/          # 模块：目录存在 = 启用
    __init__.py                #   必需：导出 MODULE = ModuleSpec(...)（必须轻量）
    api.py                     #   可选：自有 Blueprint + 视图
    models.py                  #   可选：自有集合
    tasks.py                   #   可选：Celery 任务
    constants.py               #   可选
    validators.py              #   可选
    config.py                  #   可选：模块自有配置
    config.local.py            #   可选：本机覆盖（gitignore）

moeflow-frontend/
  src/modules/                 # 核心：通用注册表
    index.ts                   #   import.meta.glob 扫描
  src/modules/<name>/          # 模块：目录存在 = 启用
    index.ts                   #   必需：导出 ModuleDefinition
    pages/ components/ api.ts
    locales/messages.yaml      #   可选：模块自有文案
```

### 3.2 开关：目录即启用

- **后端**：`app/modules/<name>/__init__.py` 存在即启用。没有隐藏的开关文件、没有环境变量开关。
- **前端**：`src/modules/<name>/index.ts` 存在即启用。
- 前后端**各自独立**判断（后端缺模块时前端 API 会 404，模块页面需自行降级）。
  若需要严格一致，可由模块自己提供一个 capability 端点——但这属于模块内部实现，不进核心。

> 可选扩展（不在本次范围）：允许通过环境变量 `MOEFLOW_MODULES_PATH` 指定仓库外的
> 模块根目录，使含密钥的模块完全不入库。代价是 `sys.path` 处理，收益是密钥零入库。

### 3.3 核心注册表（通用、无副作用）

```python
# app/modules/__init__.py   ← 核心，通用，不含任何具体模块名
from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import logging
import pkgutil
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModuleSpec:
    """模块自述。由模块 __init__.py 导出为 MODULE。"""

    name: str                                   # 模块标识，建议等于目录名
    task_packages: tuple[str, ...] = ()         # 供 celery autodiscover 的包路径
    queue: str | None = None                    # 期望队列；None 表示走 default
    init: Callable | None = None                # init(app) 钩子，做重活 import


def discover() -> list[ModuleSpec]:
    """扫描 app.modules 下的子包，返回启用的模块。

    只 import 各模块的 __init__.py（要求轻量），不触碰其 models/api/tasks。
    """
    package = importlib.import_module("app.modules")
    specs: list[ModuleSpec] = []
    for info in pkgutil.iter_modules(package.__path__):
        if not info.ispkg:
            continue
        try:
            module = importlib.import_module(f"{package.__name__}.{info.name}")
        except Exception:
            logger.exception("模块 %s 导入失败，已跳过", info.name)
            continue
        spec = getattr(module, "MODULE", None)
        if isinstance(spec, ModuleSpec):
            specs.append(spec)
        else:
            logger.warning("模块 %s 未导出 MODULE，已跳过", info.name)
    specs.sort(key=lambda s: s.name)
    return specs
```

设计要点：

- `discover()` 是**纯读取**：不连库、不建蓝图、不注册任务。因此可以被 §2.1 中的
  第 2 步和第 3 步安全地各调用一次。
- 单个模块导入失败只记日志并跳过，**不影响核心启动**（这是 C1 的一部分）。
- 核心不 import 任何具体模块；`app/modules/__init__.py` 里不出现模块名。

### 3.4 接线点（两处，各一行左右）

```python
# app/factory.py —— create_celery 内
from app.modules import discover as discover_modules

_specs = discover_modules()
_packages = [ ...现有硬编码列表... ] + [p for s in _specs for p in s.task_packages]
created.autodiscover_tasks(packages=_packages, related_name=None)

_routes = [ ...现有路由... ]
# 在通配 "*" 之前插入模块路由（只在模块声明了 queue 时才加）
_routes = [f"tasks.{...}"]  # 见下方说明
```

具体地，队列路由建议这样加（仍不含模块名）：

```python
existing_routes = [ ...原有列表... ]          # 结尾是 ("*", {"queue": "default"})
module_routes = [
    (f"tasks.{s.name}.*", {"queue": s.queue})
    for s in _specs
    if s.queue
]
catch_all = [r for r in existing_routes if r[0] == "*"]
others = [r for r in existing_routes if r[0] != "*"]
created.conf.task_routes = (others + module_routes + catch_all,)
```

```python
# app/factory.py —— init_flask_app 内，register_apis(app) 之后
for _spec in discover_modules():
    if _spec.init is not None:
        try:
            _spec.init(app)
        except Exception:
            logger.exception("模块 %s 初始化失败", _spec.name)
```

模块侧（重活延迟到 `init`）：

```python
# app/modules/<name>/__init__.py   ← 必须轻量：不 import models/api/tasks
from app.modules import ModuleSpec

def _init(app):
    from . import models  # noqa: F401  触发 mongoengine 注册
    from .api import blueprint
    app.register_blueprint(blueprint)

MODULE = ModuleSpec(
    name="<name>",
    task_packages=("app.modules.<name>.tasks",),
    queue=None,          # None → 走 default
    init=_init,
)
```

> 模块 API 是**自有蓝图**，因此 `app/apis/urls.py` 一行都不用改。
> 这正好修掉了 MIT 把 `if` 内联在 `urls.py` 里的粗糙做法。

### 3.5 模块契约（后端）小结

| 贡献点 | 方式 | 核心是否被改动 |
| --- | --- | --- |
| 蓝图 | `init(app)` 里 `app.register_blueprint` | 否 |
| Celery 任务 | `ModuleSpec.task_packages` | 否（核心只做通用拼接） |
| 队列路由 | `ModuleSpec.queue` | 否（核心只做通用拼接） |
| 集合/索引 | 模块自有 collection，`init` 时 `ensure_indexes()` | 否 |
| 常量 / schema | 模块内直接 import | 否 |
| 配置 | 模块自有 `config.py` + 环境变量 | 否（**不进 `app/config.py`**） |
| **项目创建事件** | `ModuleSpec.on_project_created(project)` | 否（核心只广播通用事件，见 D14） |
| 站点设置 | **不允许**写 `SiteSetting` | — |
| 数据迁移 | **不使用** `app/migrations/versions/` | 否 |

> `on_project_created` 是**唯一**的领域事件钩子，且它是「什么时候」而非「做什么」。
> 不允许新增 `on_archive_import` 一类的专用钩子——那会把 C2 变成空话。
> 该约束由 `tests/base/test_modules_wiring.py::GenericEventSurfaceTest` 强制。

**必须留在核心的东西**（模块化消解不掉，需明确接受）：

1. i18n 目录（后端 `app/translations/*.po`，前端 `messages.yaml` → JSON）。
2. `SiteSetting` 单例文档——模块不得往里加字段，需要站点级配置时用模块自有集合。
3. 数据库连接、鉴权、RBAC、CORS 等基础设施。
4. 迁移器本身（模块不用它，但它不因此改变）。

### 3.6 模块契约（前端）

```ts
// src/modules/registry.ts   ← 核心，通用，**纯逻辑**（无 import.meta，可单测）
export interface ModuleMenuItem { name: string; icon: IconProp; path: string }
export interface ModuleRoute    { path: string; component: FC }
export interface ProjectTopSlot { component: FC<{ projectID: string }> }

export interface FrontendModule {
  name: string;
  menuItems?: ModuleMenuItem[];
  routes?: ModuleRoute[];
  projectTopSlots?: ProjectTopSlot[];
}

export function selectModules(found: Record<string, ModuleModuleShape | undefined>): FrontendModule[]
export function collectMenuItems(modules: readonly FrontendModule[]): ModuleMenuItem[]
export function collectRoutes(modules: readonly FrontendModule[]): ModuleRoute[]
export function collectProjectTopSlots(modules: readonly FrontendModule[]): ProjectTopSlot[]
```

```ts
// src/modules/index.ts   ← 核心，通用，**唯一的构建期扫描点**
const found = import.meta.glob('./*/index.ts', { eager: true }) as Record<string, ModuleModuleShape | undefined>;
export const enabledModules = selectModules(found);   // name 排序，保证顺序稳定
export const moduleMenuItems = collectMenuItems(enabledModules);
export const moduleRoutes = collectRoutes(enabledModules);
export const moduleProjectTopSlots = collectProjectTopSlots(enabledModules);
```

> **为什么拆成两个文件**：`import.meta` 在 jest（ts-jest/commonjs）下会抛
> `SyntaxError: Cannot use 'import.meta' outside a module`。把纯逻辑留在 `registry.ts`
> 后，注册表逻辑可脱离构建期 API 直接单测。这是实施期发现的必要调整（§11.2（1））。

四处小改（各自都是通用循环，不含模块名）：

1. `pages/Dashboard.tsx`：在既有 `<Switch>` 中插入 `moduleRoutes` 渲染的 `<Route>`。
2. `components/dashboard/DashboardMenu.tsx`：把 `moduleMenuItems` 追加进现有列表。
3. `pages/ProjectFiles.tsx`：在项目页顶部渲染 `moduleProjectTopSlots`，
   每个插槽只传 `projectID`（核心的数据形状，不是模块的领域接口）。
4. `scripts/generate-locale-json.ts`：额外读 `src/modules/*/locales/messages.yaml`
   并与核心 `messages.yaml` 合并（模块 key 与核心冲突时**直接抛错**，不静默覆盖）。

> 第 4 条是为了让**核心 `messages.yaml` 保持不含模块文案**。
> 它改的是构建脚本而非运行时核心；脚本对空的模块目录是通用的（扫到 0 个即无操作）。
>
> ⚠️ 生成的 `src/locales/*.json` 是**提交进仓库的产物**，因此 `build` 必须重新生成它，
> 否则删掉模块后模块文案会残留在产物里。已加 `prebuild`（§11.2（4））。

### 3.7 与既有 `partner search` 的关系

现有「被查询」接口（`app/apis/partner_search.py`）**是 C1 当前最大的违反者**：
它向 `SiteSetting` 注入了 4 个字段，并牵动 validator、site_setting API、
前端 admin 页与文案。它是**第二个模块候选**（§7）。

---

## 4. 通用性验证（交付验收标准）

在 `app/modules/` 与 `src/modules/` 中**只保留 `__init__.py` / `index.ts`**（即零模块）时，必须满足：

| # | 检查项 | 期望 |
| --- | --- | --- |
| 1 | `app/config.py` | 与引入模块系统之前逐字节一致（无模块相关配置） |
| 2 | `app/apis/urls.py` | 无模块相关 import，无模块蓝图 |
| 3 | `app/models/site_setting.py` | 无模块字段 |
| 4 | `app/migrations/versions/` | 无模块迁移 |
| 5 | `discover()` | 返回 `[]` |
| 6 | 启动 | `gunicorn` 与两个 celery worker 正常起来，日志无模块报错 |
| 7 | `celery.conf.task_routes` | 与引入之前等价（模块路由为空） |
| 8 | 前端构建 | `vite build` 通过；`import.meta.glob` 返回空对象 |
| 9 | 前端路由/菜单 | 与引入之前一致（无多出条目） |
| 10 | `messages.yaml` | 不含任何模块文案 |
| 11 | 测试 | `tests/` 全绿（模块测试也应在零模块时被跳过或不存在） |

此外建议在 CI 增加一条**反向检查**：核心文件中不得出现任何模块名
（例如对 `app/config.py`、`app/apis/urls.py`、`app/models/site_setting.py`
grep 模块名清单，出现即失败）。这是 C1 唯一可靠的自动化保障。

### 4.1 逐项落实状态

| # | 检查项 | 状态 | 依据 |
| --- | --- | --- | --- |
| 1 | `app/config.py` | ✅ 更严格 | 归档专属配置已迁出模块，仅留与 `secrets.py` 共享的加密密钥 |
| 2 | `app/apis/urls.py` | ✅ | 归档路由已迁出；剩余 `partner_search` 属 §7 待迁移 |
| 3 | `app/models/site_setting.py` | ⏳ | 仍有 4 个 `partner_search` 字段（§7） |
| 4 | `app/migrations/versions/` | ✅ | 无模块迁移 |
| 5 | `discover()` 返回 `[]` | ✅ | 零模块时实测返回空列表 |
| 6 | 启动无模块报错 | ✅ | 模块 init 失败被 try/except 隔离并记日志 |
| 7 | `task_routes` 等价 | ✅ | `test_modules_wiring.py::TaskRoutesTest` 复算并比对 |
| 8 | 前端构建通过 | ✅ | `vite build` 通过；零模块时 glob 返回空对象 |
| 9 | 前端路由/菜单一致 | ✅ | 零模块时收集结果为 `[]` |
| 10 | `messages.yaml` 无模块文案 | ✅ | 模块文案走模块自己的 yaml，冲突即报错 |
| 11 | **零模块时测试全绿** | ✅ | **实测 75 passed / 10 skipped / 0 failed**（见下） |

**关于第 11 项（原本是个真问题）**：模块测试天然要 import 模块，模块目录一删，
这些 import 就在 **collection 阶段**抛 `ModuleNotFoundError`，pytest 报收集错误并
**中断整个套件**——不是优雅跳过。已加 `tests/modules/__init__.py::requires_module()`：
在收集期按文件系统判断模块是否存在，不存在则整文件 `pytest.skip`。

同时把**通用机制**的测试与**模块专属**的测试分开：

- `tests/base/test_modules_generic_event.py`：只测通用事件机制，**不 import 任何模块**，
  零模块时照常运行。
- `tests/modules/*`：依赖具体模块，零模块时整文件跳过。

双向实测（把两个模块目录移走再跑）：

| 状态 | 结果 |
| --- | --- |
| 两个模块都在 | `tests/modules/` + `tests/base/` → **197 passed** |
| 零模块（只剩 `app/modules/__init__.py`） | 同上范围 → **75 passed, 10 skipped, 0 failed** |

> 第 1 项的实际结果**强于**原定标准。原标准是「与引入模块系统前逐字节一致」，
> 而归档迁移后 `app/config.py` 反而**比引入模块系统之前更通用**
> （少了 6 项归档配置）。严格逐字节比对因此不再适用于该文件，
> 改用更实在的口径：核心文件不得出现模块名（由反向检查强制）。

---

## 5. 模块实例一：归档导入（平移试点）

**建议先平移归档导入**，因为：

- 它已经是完整垂直切片，且**不写 `SiteSetting`**，平移摩擦最小。
- 它自带测试（`tests/api/test_archive_import_api.py`、`tests/tasks/test_archive_import.py`）
  → 平移有回归保护。
- 它能验证「异步任务 + 进度轮询」这条主链路，而这正是 §6 要复用的。

需要处理的耦合有三处（比初看要多）：

**(1) 路由（`app/apis/urls.py:433-446`）。** 3 条路由现在挂在 **project 蓝图内部**，
路径形如 `/v1/projects/<project_id>/import-from-archive`。平移后有两条路：

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| A（推荐） | 模块自建蓝图，前缀与 project 相同：`/v1/projects`，自行声明 `/<project_id>/import-*` | 蓝图名不同但 URL 不变，前端零改动 |
| B | 保持挂在核心 project 蓝图 | 需要核心 import 模块视图，违反 C1 |

选 A。URL 保持不变，因此前端与测试不需修改。

**(2) `Team` 文档与团队设置（平移时最需要决策的一处）。** 见 §2.3 注：
`app/models/team.py` 有 2 个持久化字段、`app/apis/team.py` 有 55 行 helper +
核心 `TeamAPI.put` 分支、`app/validators/team.py` 有 2 个字段。

- 若模块要保留「每团队多把档案 API key + 团队级基址」，就得继续侵入核心 `Team`，
  或迁到模块自有集合（则需模块自带管理页）。
- 若按 §6 的做法（密钥放模块本地配置），则这部分侵入可以整体消除，
  但会**失去多团队多 key 轮换**能力——这是一个真实的产品取舍，需拍板。

**(3) 任务注册的隐性缺陷（平移时必须一并修掉）。**
`app.tasks.archive_import` **不在** `autodiscover_tasks(packages=[...])`，也不在
`task_routes` 里。它能工作纯属 import 副作用链：

```
app/__init__.py:41 init_flask_app()
  → app/apis/__init__.py:38 register_apis()
    → from . import urls
      → app/apis/urls.py:51  from app.apis.archive_import import ...
        → app/apis/archive_import.py:12  from app.tasks.archive_import import import_archive_from_gallery
```

**删掉 `urls.py:51` 那一行 import，这个任务就会在 worker 里静默消失。**
§2.3 的「`app/factory.py`：—」是**缺陷而非优点**。
平移为模块时，正好由 `ModuleSpec.task_packages` 显式登记，顺手修掉这个脆弱点。
（也说明 §3.4 的接线不是「可选优化」，而是把既有隐患转正。）

### 5.1 平移结果（已完成）

| 文件 | 迁移前 | 迁移后 |
| --- | --- | --- |
| 视图 | `app/apis/archive_import.py` | `app/modules/archive_import/api.py` |
| 模型 | `app/models/archive_import.py` | `app/modules/archive_import/models.py` |
| 任务 | `app/tasks/archive_import.py` | `app/modules/archive_import/tasks.py` |
| 常量 | `app/constants/archive_import.py` | `app/modules/archive_import/constants.py` |
| 校验器 | `app/validators/archive_import.py` | `app/modules/archive_import/validators.py` |
| 配置 | `app/config.py` 里 6 项 | `app/modules/archive_import/config.py` |
| 路由 | 核心 `urls.py` 内联 3 条 | 模块自建蓝图注册 3 条（**URL 逐字不变**） |
| 任务注册 | 靠 import 副作用 | `ModuleSpec.task_packages` **显式登记** |

全部用 `git mv` 迁移，历史保留。集合名由类名决定（`archive_import_task`），
**与模块路径无关**，因此已落库数据不受影响（有测试锁定）。

核心净减少：`app/apis/urls.py` −17 行、`app/config.py` −29 行。

### 5.2 三处需要说明的判断

**（1）`Team` 的多 key 配置：不迁（需求方拍板）。**

§5(2) 原本提出「密钥改走模块本地配置即可消除核心侵入」。需求方明确否决：

> 保留 key 轮换功能，但还是保留原来保存在服务器数据库中，因为已经做了 key 编辑 ui 了，
> 而且需要方便成员填写自己的 key。

因此 `Team.archive_api_keys` / `archive_api_url`、`_apply_archive_api_keys`、
团队设置页全部**原样留在核心**。模块只**读取**这两个字段。
这是「共享数据形状」而非「共享领域接口」，C2 允许。

**（2）外部地址校验上移到 `app/utils/external_url.py`。**

`normalize_archive_api_url` 被核心 `team.py` 与归档任务**同时**使用。若留在模块里，
核心就会反向 import 模块（违反 C1）。它本身与归档无关（HTTPS 校验、禁止内网/保留
地址、主机白名单），因此上移为通用工具并改名 `normalize_external_api_url`；
模块内保留旧名 `normalize_archive_api_url` 作为薄封装，既有调用点零改动。

**（3）模块配置通过注入 `app.config` 生效，而非改读环境变量。**

`celery.conf.app_config` 与 `app.config` 是**同一个 dict**，任务用
`config.get("ARCHIVE_MAX_ZIP_BYTES")` 读取。因此模块在 `init(app)` 时把默认值
**注入 `app.config`**（只写不存在的键），任务代码一句都不用改，
测试里 `self.app.config["ARCHIVE_..."] = ...` 的既有写法也照常生效。

⚠️ **环境变量名与单位必须逐字保留**：平移前就是「环境变量名 ≠ 配置键名」，
且 `ARCHIVE_MAX_ZIP_BYTES` / `ARCHIVE_MAX_ENTRY_BYTES` 以 **MB** 计而配置值是**字节**，
`ARCHIVE_MAX_ZIP_UNCOMPRESSED_BYTES` 的环境变量名更是 `..._UNCOMPRESSED_MB`。
若"顺手统一"，线上已设置的值会静默变成原来的 1/1048576。已有专门测试锁定单位。

`ARCHIVE_API_KEY_ENCRYPTION_KEY` **故意留在核心**：`app/utils/secrets.py` 是通用
加解密工具，核心 `Team` 依赖它。

### 5.3 一处被测试纠正的判断（关于队列）

最初我给模块声明了 `queue="output"`，理由是「与既有 output 队列约定一致」。
写完测试才发现这是错的：

```
任务名            tasks.archive_import_task   （历史命名，点号后无后缀）
模块路由匹配模式  tasks.archive_import.*
fnmatch("tasks.archive_import_task", "tasks.archive_import.*") -> False
```

即该模式**匹配不到**这个任务名。声明 `queue="output"` 只会新增一条永不命中的路由，
任务仍落到通配 `*` → **default**。而平移前的行为本来就是 default。
与其留一个误导性声明，不如如实标 `queue=None` 并写明原因。
（若日后确要改到 output，那是一次独立的运维决策，涉及哪个 worker 消费，不属本迁移。）

---

## 6. 模块实例二：外组作品检索（查询他人）

### 6.1 数据源与硬约束

来源：紫藤（ziteng）合作方检索 API，接入包见 `外组作品检索API/`（当前位于工作区根，
在 git 仓库之外）。

| 项 | 值 |
| --- | --- |
| 端点 | `GET https://partner-api.ziteng.org/partner-api/v1/works/search` |
| 鉴权 | `Authorization: Bearer <专用密钥>`（`shoreline-api.key`，仅 HTTPS，密钥不得进前端） |
| 参数 | `q`（必填 1–240）、`author`、`page`（每页最多 10）、`include_inactive`、`revision` |
| **频率** | **全接口共享每秒 1 次**，所有密钥、重复查询与分页共用；建议串行、间隔 ≥1.1s |
| 错误 | 429（`Retry-After: 1`）、409（`catalog_changed`，须丢弃本轮分页从第一页重查）、503 |
| 索引 | 正常每 30 秒刷新；`revision` 用于锁定同一版本翻页 |
| 返回 | 每条仅 8 字段：`id`/`reference`/`display_title`/`original_title`/`author`/`circle`/`state`/`stage` |
| **范围** | 默认只含 `in_progress` + `published`；`withdrawn` 需 `include_inactive=true`；**未立项 / 好本待入任何参数都取不到** |

**关键语义**：文档明确「**撞车判断、相似度、去重、合作识别、展示方式等功能由对方自行实现**」。
旧版的 `verdict`/`relationship`/`match_type`/`same_title` 等结论字段**已被移除**，
记录 ID 也已切换为逐行 ID。→ **不要依赖任何结论字段**，全部本地实现。

**这条 1 req/s 的全局配额是本模块的主导约束**：
它使「面向用户的实时交互查询」不可行（几个人同时点就打满，且会与其它消费同一配额的
脚本互抢）。因此本模块**必须异步**——但不必是定时的，采用 §6.2.2 的懒查询即可。

#### 6.1.1 实测记录（2026-09-22，用接入包内的密钥验证）

状态：**接口可用**（`ok: true`），文档描述的行为逐条实测通过。

**⚠ 最重要的发现：三个关键词在紫藤侧全都有条目。**
「查不到」不等于「没有」——**默认范围会隐藏条目**，且有一类条目任何参数都取不到。

| 关键词 | 默认范围 | 加 `include_inactive=true` | 真实情况 |
| --- | --- | --- | --- |
| `六畳一間の魔法少女` | `total: 0` | **`total: 1`** | ref=263，`state=withdrawn`，`stage="撞车不做"`（对方因撞车已放弃） |
| `魔法少女敗北実現委員会` | **`total: 1`** | `total: 1` | ref=385，`published`，正常可见 |
| `Healing Elf` | `total: 0` | **`total: 0`** | 任何范围都取不到 → 未立项 |
| `Clear Sound` | `total: 0` | **`total: 0`** | 任何范围都取不到 → 未立项 |

两个未立项关键词的探测细节（`Healing Elf`、`Clear Sound`）：

- 变体全部为 0：`HealingElf`、`healing elf`、`HEALING ELF`、`ヒーリングエルフ`、
  `癒しのエルフ`；`ClearSound`、`clear sound`、`CLEAR SOUND`、`クリアサウンド`。
- 子串同样为 0（`Clear`、`Sound`、`ound`），而同期对照测试证明检索**健康**：
  `ShiBoo` → 2、`Ixy` → 2、`Elf`/`ELF` → 1、`DL` → **135**、`the` → 3、`no` → 14。
  → 因此零结果**不是**检索故障，确认是"未立项"。
- 结论：**未立项 / 好本待入的条目在接口层面完全不可见，且无任何参数可绕过。**

第 1 条完整记录：

```json
{
  "id": "zt-5ec79bd14c4b1bd7",
  "reference": "263",
  "display_title": "【263】六畳一間の魔法少女",
  "original_title": "六畳一間の魔法少女",
  "author": "",
  "circle": "",
  "state": "withdrawn",
  "stage": "撞车不做"
}
```

> **这个发现直接改变了本模块的设计前提**，见 §6.1.2。它比编码问题严重得多：
> 我最初的测试只查了默认范围，把「被隐藏」误读成了「不存在」。

其余行为验证（逐条实测）：

| 验证项 | 结果 |
| --- | --- |
| ASCII 子串检索 | 正常（`ShiBoo` → 2 条、`Ixy` → 2 条） |
| 大小写 | **不敏感**（`Elf` 命中 `逃亡ELF11`） |
| `q` 为空 | 400 `invalid_query` |
| `q` = 241 字符 | 400 `invalid_query` |
| `page=0` | 400 `invalid_page` |
| 翻页（带正确 `revision`） | 正常 |
| 翻页（带**错误** `revision`） | **409 `catalog_changed`** |
| `author` 附加筛选 | 生效（`q=魔法少女` 20 条 → `+author=Ixy` 收窄到 1 条） |
| `include_inactive=true` | `scope` 增加 `withdrawn` |
| `page_size` | 固定 10 |
| **`total` 是否有上限** | **无上限**。`q='d'` → 130、`q='l'` → 122、`q='の'` → 200；且完整走完 `q='a'` 的 5 页（累计 50 条，与 `total` 一致） |
| 深翻页 | 正常（`q='の'` 的 page 2/3 正常推进） |

命中样例（供字段形态参考）：

```json
{
  "id": "zt-e14e0dab581daefa",
  "reference": "385",
  "display_title": "【385】[ShiBoo! (Ixy)] 魔法少女敗北実現委員会 (ブルーアーカイブ) [DL版]",
  "original_title": "魔法少女敗北実現委員会 (ブルーアーカイブ)",
  "author": "Ixy",
  "circle": "ShiBoo!",
  "state": "published",
  "stage": "已上传"
}
```

> **校准 §6.2.1 的提取规则**：`original_title` 剥离了编号和 `[DL版]`，
> 但**保留了作品系列括号** `(ブルーアーカイブ)`。本模块的提取规则应与之一致——
> 即**只剥离编号与版本标签，保留系列/作品括号**（它是重要的区分信息，不应丢弃）。

> **对匹配的现实约束**：高频词命中量大——`魔法少女` → 20 条（2 页），
> `の` → 200 条（20 页）。因此「有疑似 → 列出项目名」**必须经本地匹配收敛**，
> 不能把候选全量抛出。建议设展示上限（如 Top 5）并按相似度排序（呼应 C3）。

#### 6.1.2 查询范围：只查已立项的

**决定（需求方确认）：默认只查已立项的条目，`withdrawn` 不查。**

紫藤的可见范围：

| 范围 | 何时可见 | 本模块是否查 |
| --- | --- | --- |
| `published`（已发布） | 默认 | ✅ 查 |
| `in_progress`（在制） | 默认 | ✅ 查 |
| `withdrawn`（已终止 / 撞车不做） | 仅 `include_inactive=true` | ❌ **不查** |
| 未立项 / 好本待入 | 任何参数都取不到 | ❌ 取不到 |

即：**查询一律不带 `include_inactive`**（等同 `false`），使用紫藤的默认范围。

**观察到的 `state`/`stage` 组合**（采样自 `の`、`魔法少女` 的前几页）：

| `state` | `stage` |
| --- | --- |
| `published` | `已上传` |
| `in_progress` | `翻译中`、`嵌字中` |
| `withdrawn` | `撞车不做` |

> **被排除的信号（已知且接受）**：实测中 `withdrawn` 条目占 10–15%
> （`の` 175→200、`魔法少女` 20→24、`エルフ` 12→14），其 `stage` 为「撞车不做」——
> 语义是「对方曾做过但因撞车而放弃」。**不查 `withdrawn` 意味着拿不到这个信号。**
> 这是需求方的明确取舍，本模块不为此做补偿（不加提示、不加说明）。
>
> 若日后想启用，只需把模块配置 `PARTNER_SEARCH_INCLUDE_INACTIVE` 置真
> （§6.2.3 已把它设计为配置项，默认 `false`），无需改代码。

**无条件成立的一条**：`total: 0` 绝不能解释为「无撞车」。
它只意味着「在可查询范围内没有条目」。必须把三态**明确区分**（呼应 §6.2.3）：

- `未查到` —— 可查询范围内无条目（**不代表不存在**，可能未立项或已撤回）
- `有疑似` —— 有候选，交本地匹配
- `查询失败` —— 429/503/超时，**不得**降级为「未查到」

#### 6.1.3 未立项条目的处理：只记录，不区分

**决定（需求方确认）：默认只查已立项的条目，不尝试检测未立项情形。**

理由：未立项条目（`Healing Elf`、`Clear Sound`，以及此前的 `六畳一間の魔法少女` 若尚未立项）
在接口层面**完全不可见**，无参数可绕过（见 §6.1.1 的变体与子串探测）。
因此：

- **不为此增加机制**。不做"未立项检测"、不做额外数据源、不做启发式猜测。
- **不在 UI 上区分**「查过但未立项」与「确实不存在」——两者在接口看来完全一样，
  任何区分都是臆测。
- **仅记录该情形存在**（本节），供日后查阅。若将来紫藤开放未立项查询，再评估。

> 这是**已知且接受的盲区**，不是待修缺陷。需求方原流程
> 「没有疑似重复 → 不弹出提示」对此情形照常适用：未立项条目查不到，因而不弹提示。

**盲区会随时间自行缩小（实测佐证）**：`Clear Sound` 在 2026-09-22 早先探测时
`total=0`（未立项），同日晚些时候再测已变为：

```
state: in_progress | stage: 翻译中
original_title: Clear Sound EP.1 Gambit + Clear Sound EP.1 Gambit制作記
```

即项目一旦立项（哪怕只到「翻译中」），本模块**立刻就能查到**，
无需任何代码改动。这印证了「默认只查已立项」这一取舍的合理性：
盲区只覆盖「尚未立项」的窗口，而非永久不可见。

> 反过来说，**`total=0` 的语义必须谨慎**：它表示「此刻在可查询范围内没有条目」，
> 不等于「这个作品不存在」。因此 UI 在「未查到」时**什么都不显示**（而不是显示
> 「无撞车」这类断言），避免给出超过数据支撑范围的保证。

### 6.2 目标流程（需求方确认）

```
创建项目表单（已有 name 字段 = 项目名；已有 galleryUrl 字段可选）
  │
  ├─ 提交 → 创建项目（行为不变）
  │
  └─ 触发模块的「新项目查重」
       ├─ 后端从项目名正则提取检索标题
       ├─ 入队查询任务（异步！不阻塞创建流程）
       ├─ 查询紫藤 API（**不带 `include_inactive`**，复用已落库缓存）
       ├─ 本地匹配 → 产出「疑似重复」列表
       │
       └─ 前端在项目页顶部轮询结果：
            ├─ 未查到 → 不显示任何提示（与归档导入提示互不干扰）
            ├─ 有疑似 → 显示：疑似重复的项目名列表（含对方状态）
            └─ 查询失败 → 明确报错（**不得**降级为"未查到"）
```

**关键设计决定（本次拍板）**：

| 决定 | 取值 | 理由 |
| --- | --- | --- |
| **D-A 查询时机** | **异步**，创建后入队 | **不可同步**：全局 1 req/s + 每页 10 条，同步会阻塞创建流程 ≥1.1s 且翻页不可控。与归档导入同构（都是创建后异步 + 轮询）。 |
| **D-B 检索标题来源** | 从**项目名**正则提取，后端执行 | 需求确认。不在前端做（前端无权决定匹配语义）；保留人工修正余地（见 §6.2.1）。 |
| **D-C 匹配执行位置** | **本地**（拿缓存比对） | 紫藤只做关键词检索、不给结论；且本地匹配零请求，不消耗配额。 |
| **D-D 缓存策略** | **懒查询 + 落库**（见 §6.2.2） | 规避「全量同步耗时」问题，见下。 |
| **D-E 提示位置** | 项目页顶部，**有结果才渲染** | 未查到则组件返回 `null`，不与归档导入 `<Alert>` 冲突。 |
| **D-F 密钥位置** | 模块本地配置（`D2 = 放弃多 key 轮换`） | 保证 §5 平移与核心零侵入（C1）。 |
| **D-G 检索范围** | **不带 `include_inactive`**（只查已立项的） | 需求方确认：`withdrawn` 不查。代价是拿不到「撞车不做」信号（§6.1.2）。 |
| **D-H 三态语义** | 未查到 / 有疑似 / 查询失败，**严格区分** | `total: 0` ≠ 无撞车；未立项条目结构上不可见（§6.1.2）。 |

#### 6.2.1 检索标题提取

从项目 `name` 用正则提取「作品原名」，去掉常见包装：

- 编号前缀/后缀（`【999】`、`[999]`、`#999`）
- 社团/作者括号（`[社团 (作者)]`、`[Ixy]`、`(作者)`）
- 版本标签（`[DL版]`、`[中国翻訳]`）
- 卷号**保留**（`Episode7`、`第3巻` 属于区分信息）
- **系列/作品括号保留**（`(ブルーアーカイブ)`）——见 §6.1.1 实测校准

紫藤的 `original_title` 实测为「剥离编号与 `[DL版]` 等版本标签，但**保留系列括号**」
（例：`魔法少女敗北実現委員会 (ブルーアーカイブ)`）。
**本模块沿用同一套剥离规则**，以便两侧标题形态可比。
丢弃系列括号会显著降低匹配精度，且会让两侧形态不一致。

> 提取规则放模块内（`app/modules/<name>/title.py`），核心不感知。
> 建议保留一个可选的人工覆盖字段（模块自有集合），用于正则失效时手工指定检索词。
> 首版可先不做，但**接口留出**。

#### 6.2.2 缓存策略：懒查询 + 落库（而不是全量同步）

**问题**：§6.1 的「全量同步」在 1 req/s 下成本较高。

| 场景 | 请求数 | 耗时 |
| --- | --- | --- |
| 全量：200 个在制项目 | 200 起（+翻页） | ≥ 4 分钟 |
| 懒查询：每天新建 10 个项目 | 10 起 | ~15 秒 |

**决定：按需查询 + 结果落库**，而不是定时全量同步：

1. 新项目创建时，用其提取标题查一次紫藤，结果写入模块集合
   `partner_work`（按紫藤 `id` 去重）+ `partner_query`（关键词 → 结果 id 映射）。
2. 查询前先查本地缓存：**同关键词在 TTL 内命中则直接用，不发请求**。
3. 匹配在本地做：拿该关键词的结果集与本站项目比对。

**为什么这样更好**：

- **消除 §6.5 的调度难题**。不需要 beat、不需要宿主 cron、不需要 `manage.py` 改动
  → **D1 直接消失**，核心零侵入更彻底。
- 请求量与「新建项目数」成正比，而非「项目总数」，天然贴合 1 req/s。
- 仍然落库 → 满足「零请求做匹配」这一必要条件，匹配逻辑可任意调参。

**代价（必须接受）**：

- 缓存**只覆盖被查询过的关键词**。「两个老项目撞车」不会被发现——
  除非其中一个新建时触发过查询且关键词命中。
  → 这是刻意的取舍：**本模块定位为「新建项目时的查重护栏」，不是「全库撞车普查工具」**。
  若日后需要普查，可另加一个手动触发的批量任务（复用同一套查询与匹配代码），
  但它是**低频人工操作**，不是定时任务。

- TTL 建议 7 天（紫藤索引本身 30 秒刷新，但撞车判定不需要秒级新鲜度）。
  TTL 过期后同一关键词再查一次，成本可控。

> 这条决定把「自动化查询」从「需要运维配合的定时批处理」降级为
> 「随用户操作自然发生的按需查询」，是本次最大的简化。

#### 6.2.3 模块配置（环境变量 / 独立配置文件）

**需求方确认：各模块的配置用环境变量或单独的配置文件控制。**

模块配置**不进核心 `app/config.py`**（那会违反 C1）。优先级从高到低：

| # | 来源 | 用途 |
| --- | --- | --- |
| 1 | **环境变量** | 生产部署首选（密钥不入库、可被 compose 注入） |
| 2 | **模块本地配置文件** `app/modules/<name>/config.local.py` | 开发/单机；**必须 gitignore** |
| 3 | 模块内置默认值（`config.py` 中的 `DEFAULTS`） | 保证零配置可运行 |

建议接口（模块自持，核心不感知）：

```python
# app/modules/ziteng_partner/config.py —— 模块自有
def get(name: str, default=None): ...   # 环境变量 → config.local → 默认值
```

配置项清单：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `ZITENG_PARTNER_API_KEY` | 无（缺失则模块不启用） | 密钥。**不得进 `app/config.py` / `SiteSetting` / `Team` / 前端** |
| `ZITENG_PARTNER_API_URL` | 官方端点 | 便于测试打桩 |
| `ZITENG_PARTNER_INCLUDE_INACTIVE` | **`false`** | 是否查 `withdrawn`。默认关闭（§6.1.2）；开启即恢复「撞车不做」信号 |
| `ZITENG_PARTNER_CACHE_TTL` | `7d` | 关键词缓存有效期 |
| `ZITENG_PARTNER_MAX_PAGES` | `3` | 翻页上限（防高频词占满配额） |
| `ZITENG_PARTNER_TIMEOUT` | `20s` | 单请求超时 |

**开关 = 配置是否存在**（呼应 C1 的"目录即开关"）：
`ZITENG_PARTNER_API_KEY` 缺失时模块 `init` 记 INFO 并跳过注册，站点的核心功能照常。

安全与正确性要点：

- **查询默认不带 `include_inactive`**（D-G）。
- **三态严格区分**（D-H）：
  - `未查到`（`total: 0`）→ 前端**不渲染**
  - `有疑似` → 渲染列表
  - `查询失败`（429/503/超时/网络错误）→ **明确报错**
  **绝不能把查询失败降级为「未查到」**——那会让用户以为安全。这是本模块最危险的失败模式。
- **429**：读 `Retry-After: 1` → 等待 → 重试；仍失败则记「查询失败」。
- **503**：记失败并保留上次成功结果；**不得**把索引不可用当作「无条目」。
- **409（`catalog_changed`）**：翻页期间索引变化 → 丢弃本轮分页，从第一页重查。
- **翻页**：携带第一页返回的 `revision`，`next_page=null` 结束。
- **翻页上限**：`total` 无上限，高频关键词可达 20 页（`の` → 200 条）。
  超过 `ZITENG_PARTNER_MAX_PAGES` 即停止并标注「结果可能不完整」。
- **单实例串行**：同一时刻只允许一个查询任务（模块自带锁，见 §6.4）。

#### 6.2.4 匹配与展示

候选拿到后需本地收敛（实测高频词会返回 20+ 条）：

| 候选状态 | 展示建议 | 理由 |
| --- | --- | --- |
| `in_progress` | **最高优先级**，明确标红 | 对方正在做 → 直接撞车 |
| `published` | 正常展示 | 对方已出成品 → 可能重复翻译 |

**排序**：`in_progress` > `published`，同级按标题相似度降序。

**展示上限**：Top 5（D9）。超出部分以「另有 N 条」折叠呈现，不直接铺开。

> `withdrawn` 条目不在默认范围内，因此不参与匹配（§6.1.2）。
> 若把 `ZITENG_PARTNER_INCLUDE_INACTIVE` 置真，则需扩展本表以处理
> `withdrawn` + `stage="撞车不做"`（应作为强信号展示）。**默认不做。**

### 6.3 进度提示 UI：复用模式，但修正两处假设

> 需求方原话：「应该可以复用归档下载那部分的弹窗」。

**实测：归档导入的进度 UI 不是弹窗。**
`ArchiveImportProgress.tsx` 渲染的是 antd `<Alert>`，由
`pages/ProjectFiles.tsx:152` **内联**在项目文件页里。
该流程里真正的 `<Modal>` 是 `ProjectCreateForm.tsx:357` 的「创建须知」弹窗，与进度无关。

> **好消息（相比 §6.2 早期的全量同步设想）**：新流程是**项目级**的
> （某个新项目是否有疑似重复），与归档导入的粒度**完全一致**。
> 因此可复用的程度比原先预期高得多——「绑定单个 project」不再是问题，
> 而是正好匹配。提示同样挂在 `ProjectFiles.tsx` 顶部，与归档导入 `<Alert>` 并列。

可复用要素：

| 可复用要素 | 位置 | 说明 |
| --- | --- | --- |
| 任务文档 + 状态机 | `app/models/archive_import.py` + `app/constants/archive_import.py` | `IntType` 枚举 + `RUNNING` 元组 |
| 原子领取 | `ArchiveImportTask.claim()` | `QUEUED → RESOLVING` 的 `update_one`，保证单 worker 领取 |
| 局部进度写 | `ArchiveImportTask.set_progress(...)` | `update()` 避免 save 并发覆盖 |
| 派发 + 同步回退 | `app/tasks/archive_import.py:439` | `_FORCE_SYNC_TASK`（= `TESTING`）或 broker 不可达时同步执行——**测试无需 broker** |
| 轮询端点 | `GET .../<project_id>/<module>-task` → `{task: {...}}` | 前端 `setTimeout` 递归轮询 |
| 前端轮询循环 | `ArchiveImportProgress.tsx:43-82` | 3s 间隔；任务结束即停止 |
| 「不再提示」存库 | `task/dismiss` + `dismissed` 字段 | 换设备不再出现 |

**需按本模块调整的部分**：

- **三态而非两态**：归档导入是「进行中 / 成功 / 失败」；本模块是
  **「未查到 → 不渲染」/「有疑似 → 列出项目名（含对方状态）」/「查询失败 → 明确报错」**。
  `Task.status` 的 `SUCCEEDED` 需再区分子状态（`CLEAR` / `SUSPECTED`），
  或直接把匹配结果存进任务文档由前端判断。
- **未查到必须静默**：`return null`，不占用顶部空间（需求明确要求）。
- **有疑似展示项目名列表**：用 `<Alert type="warning">` + 列表，
  **同时显示对方的 `state`/`stage`**（尤其 `撞车不做`），
  链接到项目页（若命中本站项目）或纯文本展示紫藤侧 `display_title`。
- **查询失败必须显式**：`<Alert type="error">` + 重试按钮，
  **不得**静默、**不得**显示为"未查到"。
- **不对未立项做特殊处理**：未立项条目接口不可见，UI 不区分、不提示、不加兜底链接
  （§6.1.3）。措辞保持中性即可，无需声明"可能存在未登记的条目"。
- 完成时**不** `window.location.reload()`（归档导入是为了刷新文件列表，本模块无此需要）。
- 轮询出错时**不重排下一次轮询**（归档 `:71-73` 的 catch 里没有 `setTimeout`），
  即一次网络抖动会永久终止轮询直到组件重挂载。**模块应修掉这个行为。**
- 它包含一个**疑似线上 bug，不要照抄**（见下）。

**⚠ 疑似线上 bug：`ArchiveImportProgress.tsx` 的进度条可能从不渲染。**
该组件声明并读取 `task.totalPages` / `task.completedPages`（`:15-16`, `:156-160`），
但后端 `to_api()` 输出的是 snake_case 的 `total_pages` / `completed_pages`。
链路上**没有任何转换**：

- 后端无 camel 转换（`app/` 内 grep `camel` 为空，`MoeAPIView` 只提供 `current_user`）。
- axios 实例 `axios.create({ baseURL })`（`src/apis/index.ts:42-46`）无 `transformResponse`。
- `request()` 成功分支原样 `data: response.data`（`:178-182`），
  只有**错误**分支才对 message 调 `toLowerCamelCase`（`:202`）。
- `getArchiveImportTask`（`src/apis/project.ts:404-415`）无转换。

若成立，`totalPages` 恒为 `undefined` → `percent` 恒为 `undefined` →
永远走 `<Spin>` 分支，`<Progress>` 永不出现。**未实际运行 UI 验证**。
本模块不复用该组件，故不受影响；但若要顺手修复，属独立缺陷修复，不在模块化范围内。
（附带教训：`RUNNING_STATUSES`/`FAILED_STATUS` 在组件里是**内联字面量** `[0,1,2,3,4]`/`6`，
与后端枚举各写一遍，这正是 camel 漂移能悄悄存活的原因。）

**建议**：模块复制这约 40 行轮询循环并按需调整，
不把这个循环抽成核心 hook。理由：抽成 hook 就是在核心引入一个只为异步任务服务的抽象，
而目前只有 2 个使用方（归档导入 + 本模块），且形态不同（项目级 vs 站点级）。
模块复制代码完全符合 C2（模块不承诺复用/兼容），而核心保持零新增。

前端展示建议：模块自有页面（如 `/dashboard/partner-search`），
顶部用 `<Alert>` 显示同步状态（进行中 / 上次成功时间 / 失败原因 + 重试），
下方用 antd `<Table>` 列出「疑似撞车」结果。**不做交互式搜索框**——配额不允许。

### 6.4 限流：不能复用现有 limiter，但可借它的写法

站内**唯一**的限流实现是 `PartnerSearchThrottle.check_allow(key, rate_limit_seconds)`
（`app/models/partner_search.py:27-53`），Mongo 支撑的**固定窗口、按 key** 限流，
配置在 `SiteSetting.partner_search_rate_limit_seconds`（默认 **10** 秒）。

**形状不符，不能直接用**：

| | `PartnerSearchThrottle` | 本模块需要 |
| --- | --- | --- |
| 方向 | 入站（防外部打我们） | **出站**（遵守紫藤的预算） |
| 粒度 | per-key（`ip:` + `client_key`） | **全局单一预算**（文档：全接口共享每秒 1 次） |
| 窗口 | 可配置秒级 | 1 秒，且要 ≥1.1s 保险 |
| 调用方 | 仅 `app/apis/partner_search.py:121-124` | Celery 任务 |

**但它的原子写法值得照抄**（固定窗口 + `update_one` 抢占，天然抗并发）：
用一个**全局 key**、窗口 1 秒即可。另外：

- **Redis 可用**（`redis==6.4.0` 已在依赖里）——若担心 Mongo 往返或需要跨 worker 共享预算，
  可用 Redis 实现，但 Mongo 版本已足够（同一时刻只有一个查询任务在跑）。
- **关键前提**：本模块的查询任务必须是**单实例串行**的。若同时跑两个任务，
  全局预算会翻倍，必然触发 429。建议模块自带一把锁（或复用 `claim()` 式的原子领取）。
- 现有代码库**没有任何 429/`Retry-After` 处理**（归档导入对失败只换 key 重试，无退避）。
  本模块必须自己实现：读 `Retry-After: 1` → 等待 → 重试；429 不能等同于「无结果」。

### 6.5 调度：不需要调度器（D1 已消解）

**采用 §6.2.2 的懒查询后，本模块不需要任何定时调度**：

- 查询由**用户创建项目**这一动作自然触发 → 无需 beat、无需宿主 cron、无需 `manage.py` 改动。
- **因此待决策项 D1（`manage.py` 是否改动）消失**，选「不改」，核心侵入更小。
- §2.5 的「无 beat」约束对本模块**不再构成障碍**。

保留的两种可选触发器（都**不是**必须）：

| # | 可选机制 | 用途 | 代价 |
| --- | --- | --- | --- |
| 1 | 项目页手动「重新查重」按钮 | 正则失效、TTL 过期、或用户想复查 | 复用同一任务入口，前端加一个按钮 |
| 2 | 手动批量普查命令 | 一次性全库撞车排查（低频人工操作） | 需 `manage.py` 注册命令（回到 D1）**或**用一个仅管理员可调的接口 |

**建议首版只做「创建时自动触发 + 项目页手动重查」**，不做批量普查。
批量普查若日后确有需要，再单独评估——它是**低频人工操作**，不是定时任务。

> 若将来真要做批量普查，方案是在模块 `init` 里注册一个仅站点管理员可调的接口
> （而非改 `manage.py`），从而保持 `manage.py` 零改动。
> 这比引入 celery-beat 容器轻得多，也避免动服务器 compose。

### 6.6 待决策项的状态更新

| 原编号 | 问题 | 本次决定 |
| --- | --- | --- |
| D1 | `manage.py` 是否为模块改动 | **不需要**（懒查询消解了调度需求） |
| D2 | 团队档案 API key 是否保留多 key 轮换 | **放弃**——模块密钥进本地配置，核心 `Team` 零侵入 |

---

## 7. 第二个候选模块：现有 partner search（被查询）

现有 `app/apis/partner_search.py`（`/v1/partner-search-query-entry`）是**对外提供**本站
进行中项目的接口——与 §6 方向相反。

**它与 §6 不应共用同一个接口。** 两者共享的只有「partner」这个词：

| | 被查询（本模块） | 查询他人（§6） |
| --- | --- | --- |
| 形态 | 我们**提供**的 HTTP 端点 | 我们**消费**的外部客户端 |
| 状态 | 无状态 | 需缓存、需调度、需串行队列 |
| 凭据 | 无 | 私密密钥、全局配额 |
| 数据 | 本站 `Project` | 他站登记资料 |

因此模块之间**不定义共享的领域接口**，只共享 §3.3 的注册机制。

**建议把它也平移为模块**，理由是它是 C1 当前最大违反者（§3.7）：

- 移除 `SiteSetting` 的 4 个字段（`:31-39`、`:70-73`）、validator 4 项、site_setting API 4 处赋值。
- 移除前端 `AdminSiteSetting.tsx` 的相关表单与 3 处文案。
- **但其站点级配置（启用开关、团队 ID 白名单、速率、条数上限）需迁到模块自有集合**，
  否则要么继续污染 `SiteSetting`，要么失去管理员可配置性。
  → 需要模块自带一个极简管理页（这会让前端工作量大于 §6）。
- **有现成测试保护**：`tests/api/test_partner_search_api.py`（9 KB），平移可回归验证。

优先级建议：**先做 §5（归档导入试点）→ 再做 §6（新模块）→ 最后再评估 §7**。
理由：§7 收益大但前端改动也大，且它现在是可用的，不紧急。

### 7.1 当前状态：**明确不做，列为待办**

需求方 2026-09-23 决定：**`partner search` 暂不平移，留作后续待办**。

因此本次交付**有意保留**这处核心耦合，并如实登记在
`tests/base/test_modules_wiring.py::CORE_FILES_PENDING_MIGRATION`：

```python
CORE_FILES_PENDING_MIGRATION = {
    "app/apis/urls.py": ("partner_search 仍是核心蓝图（§7）",),
    "app/models/site_setting.py": ("partner_search 仍有 4 个核心字段（§7）",),
}
```

> 这张表不是"忘记清理的遗留"，而是**有意的待办登记**：它让「哪些文件还不通用、
> 为什么」在代码里可见，且 `test_pending_migration_list_is_not_stale` 会在
> 该文件**变得通用**时失败，提醒把条目移到强制通用清单——这样待办不会烂掉。

**后续要做时的工作量提示**（供下次接手估算）：

1. `SiteSetting` 4 个字段迁到模块自有集合（`partner_search_enabled` /
   `_team_ids` / `_rate_limit_seconds` / `_max_limit`，见 `:31-39`）。
2. `app/apis/site_setting.py` 4 处赋值、`app/validators/site_setting.py` 4 项移除。
3. `app/apis/urls.py` 的 `partner_search` 蓝图移除，改由模块自建蓝图注册
   （URL `/v1/partner-search-query-entry` 必须保持不变）。
4. **前端要新增一个极简管理页**（原配置在 admin 站点设置里），
   `AdminSiteSetting.tsx` 有 **26 处**引用需要拆走。
5. 回归保护：`tests/api/test_partner_search_api.py`。

> ⚠️ 第 4 项是这件事的真正成本所在：它是唯一一处**需要新写前端界面**的迁移，
> 不像 §5 那样能靠「URL 不变」实现前端零改动。

---

## 8. 风险与未决问题

| # | 事项 | 影响 | 建议 |
| --- | --- | --- | --- |
| R1 | 模块导入失败被静默跳过（§3.3） | 模块静默不生效，难排查 | `init` 失败记 ERROR 日志；**并把模块名与启用状态打进启动日志**（`discover()` 结果一行 INFO），无需新增 CLI |
| R2 | 模块自有集合「失效即重建」意味着**数据不可保留** | 若某模块存入不可重建的数据会丢失 | 写进模块契约：**模块集合必须可重建**；否则必须走核心迁移链（需在评审时否决） |
| R3 | 前端模块与后端模块各自独立启用 | 可能前端有页面而后端无接口 | 模块页面必须处理 404/空数据；不依赖两者同步 |
| R4 | `import.meta.glob` 的 eager 加载 | 模块多时首屏包体增大 | 目前模块数极少；必要时改 `lazy` + `React.lazy` |
| R5 | 模块声明独立队列需新增 worker | 部署脚本 + 服务器 compose 都要改 | 默认 `queue=None` 走 `default`；确需独立队列时同步部署变更 |
| R6 | ~~「自动化」无 beat~~ | **已消解** | 懒查询不依赖调度器（§6.2.2、§6.5） |
| R7 | 紫藤 API 语义可能再变（已变过一次） | 模块需跟进 | 正是把它做成模块的理由：改动局限在模块内 |
| R8 | `外组作品检索API/` 目录位置与密钥 | 密钥泄露风险 | 移出工作区或确认不入库；密钥只进模块本地配置/环境变量 |
| R9 | ~~`manage.py` 是否为模块改动~~ | **已消解** | 懒查询不需要 CLI 命令（§6.6） |
| R10 | 团队成员多 key 配置若保留，模块必须侵入核心 `Team` | §5(2)：违背 C1，且是归档导入最贵的一部分 | **已决定放弃**多 key 轮换，密钥走模块本地配置 → 核心 `Team` 零侵入（§6.6） |
| R11 | 全局 1 req/s 预算 + `--concurrency=1` | 若并发跑两个查询任务即触发 429 | 查询任务必须单实例串行（模块自带锁）；429/`Retry-After` 必须显式处理（§6.4） |
| R12 | 现有 429 处理缺失 | 任何"失败即无结果"的简化都会把限流误判为"查无此作品" | 区分「查询失败」与「零条结果」——紫藤文档明确要求（§6.2.3） |
| R13 | **懒查询只覆盖被查过的关键词** | 两个老项目撞车不会被发现 | 刻意取舍：本模块定位为「新建时的查重护栏」，非全库普查工具（§6.2.2） |
| R14 | **标题正则提取可能失准** | 提取错 → 查不到 → 漏报（假阴性） | 首版接受；接口预留人工覆盖字段（§6.2.1）；漏报比误报安全，但需在 UI 措辞上不承诺"绝对无撞车" |
| R15 | **高频词候选量大**（实测 `魔法少女` → 20 条，`の` → 200 条） | 若直接展示会把无关项抛给用户 | 本地匹配收敛 + 展示上限（Top 5）+ 状态优先级排序（§6.2.4） |
| R16 | `total` 无上限、深翻页可用 | 单次查重的请求数是**不可预知**的 | 设翻页上限（3 页）；超时/超限归入「查询失败」，**不得**归入「未查到」（§6.2.3） |
| R17 | **未立项条目接口不可见**（实测 `Healing Elf` 取不到；`Clear Sound` 起初取不到，立项后即可见） | 这类撞车无法检测，但**盲区随时间自行缩小** | **已决定不处理**：只记录情形存在，不增加机制、不在 UI 区分、不做兜底（§6.1.3）。属已知且接受的盲区 |

## 9. 落地顺序建议（含实际进度）

1. ✅ **核心骨架**：`app/modules/__init__.py` + `factory.py` 两处接线 + `src/modules/index.ts`
   + 前端三处接线 + `generate-locale-json.ts` 扩展。
2. ✅ **验证 C1**：§4 的检查项已落成测试（零模块等价性、反向检查、通用钩子面）。
3. ✅ **平移归档导入**（§5），用现有测试回归；顺带修掉任务注册的隐性缺陷（§5(3)）。
4. ✅ **新建外组检索模块**（§6）：
   a. ✅ 模块骨架 + 落库集合 + 紫藤客户端（含 429/409/503 处理）+ 标题提取
   b. ✅ 查询任务（懒查询 + 缓存 TTL）+ 匹配逻辑
   c. ✅ 项目页顶部提示（三态）+ 创建时触发 + 手动重查按钮
5. ⏳ **评估 partner search 平移**（§7）—— 需求方 2026-09-23 决定**暂不做，列为待办**（见 §7.1）。
6. ⏳ 视需要把 MIT 从中枢的 `if`（`urls.py:612`）迁到模块形态，作为第三个实例收口。

> ✅ = 已完成，⏳ = 未开始。完成情况与偏差详见 §11。

---

## 10. 决策记录

| # | 问题 | 决定 | 状态 |
| --- | --- | --- | --- |
| D1 | `manage.py` 是否为模块加一处通用遍历？ | **不加**——懒查询不需要调度器 | ✅ 已定 |
| D2 | 团队的档案 API key 是否保留多 key 轮换？ | **放弃**——密钥进模块本地配置，核心 `Team` 零侵入 | ✅ 已定 |
| D3 | §7 的 partner search 是否本期平移？ | **不做，列为待办**——需求方 2026-09-23 拍板；工作量提示见 §7.1 | ✅ 已定 |
| D4 | 模块自有集合「失效即重建」是否所有模块都接受？ | **接受**——写进模块契约（C2） | ✅ 已定 |
| D5 | 查询触发时机 | **创建项目时异步触发** + 项目页手动重查 | ✅ 已实施 |
| D6 | 缓存策略 | **懒查询 + 落库 + TTL 7 天**（非全量同步） | ✅ 已实施 |
| D7 | 提示呈现 | 未查到 → 不渲染；有疑似 → 列项目名（含状态）；失败 → 明确报错 | ✅ 已实施 |
| D8 | 检索标题提取是否剥离系列括号？ | **保留**——与紫藤 `original_title` 形态一致（§6.1.1 实测） | ✅ 已定 |
| D9 | 疑似结果展示数量 | **设上限（Top 5）+ 状态优先级排序**，不抛全量候选 | ✅ 已定 |
| D10 | 是否查询 `withdrawn` 条目？ | **不查**——只查已立项的（`published` + `in_progress`）；可用配置开启 | ✅ 已定 |
| D11 | 未立项条目如何处理？ | **只记录情形存在，不区分、不检测**——默认只查已立项的（§6.1.3） | ✅ 已定 |
| D12 | 模块配置来源 | **环境变量 → 独立配置文件 → 内置默认**；不进核心 `app/config.py` | ✅ 已定 |
| D13 | 模块未配密钥时是否注册蓝图？ | **注册**——密钥只影响查询能否执行，不影响路由是否存在 | ✅ 已实施 |
| D14 | 核心如何感知「项目创建」这一事件？ | **通用事件钩子** `on_project_created`，核心不认识任何模块 | ✅ 已实施 |
| D15 | 项目页顶部提示的注入方式 | **通用插槽** `projectTopSlots`，组件只收 `projectID` | ✅ 已实施 |
| D16 | 归档导入的 `Team` 多 key 配置是否迁走？ | **不迁，留在核心数据库**——需求方拍板：已有多 key 编辑 UI，且需方便成员填写自己的 key。模块只读这两个字段（§5.2） | ✅ 已实施 |
| D17 | 归档模块的队列 | **default（`queue=None`）**——任务名 `tasks.archive_import_task` 不匹配 `tasks.archive_import.*`，标 output 会留下永不命中的路由（§5.3） | ✅ 已实施 |

> **接口验证**：2026-09-22 用接入包密钥实测通过，详见 §6.1.1 / §6.1.2 / §6.1.3。
> **四个关键词的归类**：
> - `魔法少女敗北実現委員会` → 默认可见（`published`）→ **本模块会检出**（实测 `exact` 命中）
> - `六畳一間の魔法少女` → `withdrawn`（`撞车不做`）→ **本模块不查**（D10）；开启
>   `include_inactive` 后确认可查到，即该取舍确实生效
> - `Healing Elf` → 未立项，任何参数都取不到 → **不处理**（D11）
> - `Clear Sound` → 探测量时为未立项；**同日晚些时候立项（`in_progress`/翻译中）后即可查到**
>   → 印证 D11 的盲区会随时间自行缩小（§6.1.3）

> 遗留可选项（非阻塞）：批量全库普查（§6.5 方案 2）、标题正则的人工覆盖字段（§6.2.1）。
> 两者均可在首版之后追加，不影响核心设计。

---

## 11. 实施记录

本节记录实际落地时**与设计有出入**或**设计没料到**的地方。设计文档的价值在于事后
能对账，因此这里如实记录偏差，而不是把文档改写成「一开始就这么设计的」。

### 11.1 落地清单

**后端（`moeflow-backend`）**

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `app/modules/__init__.py` | 核心 | `ModuleSpec` / `discover()` / `log_enabled_modules()` / `notify_project_created()` |
| `app/factory.py` | 核心，改 2 处 | `init` 钩子调用；`autodiscover_tasks` + `task_routes` |
| `app/apis/team.py` | 核心，改 1 行 + 2 处调用 | 只 import 通用事件，不含模块名；归档校验器改用通用工具 |
| `app/utils/external_url.py` | 核心（新增，通用） | 外部 API 地址校验（原在归档校验器里，被核心 team.py 使用） |
| `app/apis/urls.py` | 核心，**净减 17 行** | 归档路由已迁出 |
| `app/config.py` | 核心，**净减 29 行** | 归档专属配置已迁出，仅留与 secrets.py 共享的加密密钥 |
| `app/modules/archive_import/*` | **模块一** | `api/models/tasks/constants/validators/config` |
| `app/modules/ziteng_partner/*` | **模块二** | `config/client/title/matching/models/service/tasks/api` |
| `tests/base/test_modules_registry.py` | 测试 | 注册表与发现 |
| `tests/base/test_modules_wiring.py` | 测试 | 接线等价性、C1 反向检查、通用钩子面、归档登记 |
| `tests/modules/*` | 测试 | 两个模块各自的行为与迁移接线 |

**前端（`moeflow-frontend`）**

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `src/modules/registry.ts` | 核心 | 纯逻辑：`selectModules` / `collect*` |
| `src/modules/index.ts` | 核心 | `import.meta.glob` 扫描 |
| `src/pages/ProjectFiles.tsx` | 核心，改 1 处 | 渲染 `moduleProjectTopSlots` |
| `src/pages/Dashboard.tsx`、`src/components/dashboard/DashboardMenu.tsx` | 核心，各改 1 处 | 路由与菜单 |
| `scripts/generate-locale-json.ts` | 构建 | 合并模块文案，冲突即报错 |
| `src/modules/ziteng_partner/*` | 模块 | `api/logic/ZitengCheckAlert/index/locales` |

### 11.2 与设计的偏差

**（1）`registry.ts` / `index.ts` 拆分（设计没写）**

`import.meta` 在 jest 的 ts-jest/commonjs 下直接抛
`SyntaxError: Cannot use 'import.meta' outside a module`，导致注册表逻辑无法单测。
因此把纯逻辑放进 `registry.ts`（无 `import.meta`），只让 `index.ts` 承担 Vite 扫描。
**副作用是好的**：逻辑测试不再需要构建期 API，测试跑得又快又稳。

**（2）模块密钥不再控制蓝图注册（D13，**推翻了最初实现**）**

最初写成 `init=_init if _enabled() else None`，即没配密钥就不注册蓝图。
实测发现两个问题：

- `app/__init__.py` 在 **import 时**就构建应用并调用 `init` 钩子，
  因此 `_enabled()` 的求值时机早于任何运行期配置注入，**行为依赖 import 顺序**；
- 表现为测试环境里模块「被发现」却「没有路由」，排查成本高。

改为**始终注册蓝图**，密钥只决定查询能否执行：
`/v1/ziteng-partner/config` 如实返回 `enabled: false`，查重请求以明确的失败态落库。
「未启用」与「配错了」因此可区分，且不再依赖 import 顺序。

**（3）新增两个通用扩展点（D14 / D15）**

设计只说了「注册机制」，没具体规定核心如何触发模块。落地时发现两处必须扩展：

- **后端**：项目创建后要触发查重。核心**不能**写 `if ziteng: ...`，
  于是加通用事件 `ModuleSpec.on_project_created`，核心只广播 `notify_project_created(project)`。
- **前端**：提示要出现在项目页顶部，而模块既不贡献路由也不贡献菜单，
  于是加通用插槽 `FrontendModule.projectTopSlots`，组件只收 `projectID`。

两者都守住 C2：共享的是**注册机制**，不是领域接口。
`tests/base/test_modules_wiring.py::GenericEventSurfaceTest` 会阻止将来有人往
`ModuleSpec` 上加 `on_archive_import` 之类的专用钩子。

**（4）文档里没有的 `prebuild`（前端文案残留）**

`src/locales/*.json` 是**提交进仓库的生成产物**，而 `build` 原本只是 `vite build`。
实测：删掉模块目录后重新构建，`ziteng*` 文案**仍留在产物里**——
「目录即开关」只对了一半（代码走了，文案还在）。

修法是加 `prebuild: npm run build:locale`，让文案在每次构建前从源码树重新生成。
已做**双向验证**：删目录 → 产物与源码文案均无任何 `ziteng` 痕迹；放回 → 模块代码回到 bundle。

### 11.3 测试捕获的真实缺陷

实施过程中被测试抓出的问题，均已在代码中修复：

| 缺陷 | 后果 | 发现方式 |
| --- | --- | --- |
| `ZitengCheck.claim()` 写成 `self.status = int(A) and int(B)` | 领取状态错乱，并发保护失效 | 服务层单测 |
| 蓝图 `add_url_rule` 只声明 `GET/OPTIONS`，但视图有 `post` | **手动重查接口在生产会 405** | API 测试 |
| `notify_project_created` 未包住 `discover()` | 模块扫描失败会**让项目创建失败** | 集成测试 |
| `build` 不重新生成文案 | 删模块后产物残留模块文案 | 构建产物比对 |
| 归档模块误标 `queue="output"` | 会留下一条永不命中的路由（任务名不匹配该模式） | 接线测试 + `fnmatch` 验证（§5.3） |
| `config_local.py` **未被 gitignore** | 模块密钥文件可能被提交进仓库（泄漏） | 检查忽略规则时发现（§11.6） |

### 11.4 验收结果

| 项目 | 结果 |
| --- | --- |
| 后端全量 | **616 passed, 1 skipped**；1 个 flaky 用例间歇失败（见下方说明），**非本次改动引入** |
| 后端模块相关 | **197 passed**（`tests/modules/` + `tests/base/`，两个模块都启用时） |
| 零模块状态 | **75 passed, 10 skipped, 0 failed**（§4 检查项 11，双向实测） |
| 前端测试 | **185 passed**（23 suites） |
| 前端 `tsc --noEmit` | 通过 |
| 前端 `eslint src/modules` | 通过 |
| 前端 `npm run build` | 通过 |
| 目录开关双向验证 | 通过（见 11.2（4）） |
| C1 反向检查 | 通过；`app/apis/team.py`、`app/config.py` 已移入**必须通用**清单。剩余待迁移项只有 partner_search（§7） |
| 归档 URL 一致性 | 通过（3 条路由逐字不变，前端零改动） |
| 归档配置单位保全 | 通过（MB→字节、环境变量名≠键名均有测试锁定） |

> **关于那 2 个间歇失败的用例（`test_file_model::test_upload_text_file`、
> `test_multi_target_cache::test_upload_new_revision`）**：
>
> 全量跑多次，失败组合在 `0 个` / `1 个` / `2 个` 之间变化，**同一份代码**。
> 单独跑这两个用例，无论有没有本次改动，都是稳定 `2 passed`。
> 结论：它们是**受执行顺序/环境影响的既有 flaky 用例**，与模块系统无关，
> 不构成本次交付的回归。
>
> 曾一度想把「迁移显式登记任务 → 顺带修好它们」写进结论，验证后主动否掉了这个说法：
> 在**纯净 `HEAD`**（改动全部 stash）上单独跑同样通过，故不能算作本次收益。
> 记录在此以免下次又被误判为回归或战绩。
>
> 唯一有依据的收益是 §5(3) 那条：归档任务从「靠 import 副作用注册」变为
> **显式登记**，这在结构上消除一个真实脆弱点（有测试锁定）。

**「查询失败不得降级为未查到」的守卫**（紫藤模块最危险的失败模式）已在五层各自断言：

- 客户端层：429/409/503/网络错误/非 JSON → 一律 `PartnerApiError`，**从不返回空结果**
- 服务层：`test_failure_is_not_clear`、`test_timeout_is_failure_not_clear`
- API 层：`test_failed_verdict_is_exposed_as_failed`
- 模型层：`test_set_failed_never_sets_clear`
- 前端层：`deriveAlertState` 的失败态优先于残留 `suspects` 与 `clear`

### 11.5 尚未完成（有意的待办，非缺陷）

- **§7 partner search 平移**：需求方决定暂不做（§7.1）。这是**唯一**剩余的核心耦合，
  已登记在 `CORE_FILES_PENDING_MIGRATION`，且其"未完成"状态由测试守护。
- 全库批量普查、标题人工覆盖字段（§6.5 / §6.2.1 的遗留可选项）
- 模块的 `README`（当前以模块目录内的注释代替）
- 归档任务的队列归属：目前继承平移前的 **default**（§5.3）。
  若日后要挪到 `output`，属于独立的运维决策，需要同时改任务名或另加精确路由。

> 除 §7 外，本文档描述的目标（核心骨架 + 零模块通用性 + 两个模块 + 测试）**均已完成**。

### 11.6 模块配置的两种控制方式（需求确认项）

需求要求「各个模块的配置可以用**环境变量或单独的配置文件**来控制」。两者都已支持：

**（1）环境变量** —— 适合容器/CI 部署，也是优先级最高的一档。

```
ZITENG_PARTNER_API_KEY=...
ZITENG_PARTNER_MAX_PAGES=2
ZITENG_PARTNER_INCLUDE_INACTIVE=true
```

**（2）模块自己的配置文件** —— 适合本机开发，免得每次 export。

模块目录下的 `config_local.py`（**已被 .gitignore 忽略**），
示例见 `app/modules/ziteng_partner/config_local.py.example`。
只读取其中的**大写**变量；不认识的键被忽略，不会让模块启动失败。

**优先级：环境变量 > `config.local.py` > 内置默认值。**

> ⚠️ **实施中发现并修掉的一个真实风险**：`config_local.py` 原本**不在 .gitignore 里**。
> 文档当时已经写着「该文件必须 gitignore」，但规则并不存在——而
> `app/modules/ziteng_partner/` 整个目录都是新增未跟踪状态，一旦有人按文档
> 建了真密钥文件并 `git add .`，密钥就会被提交。已在 `.gitignore` 补上
> `**/config_local.py`（`.example` 仍可提交），并保持两者都能被
> `git check-ignore` 验证。

归档模块的配置同样走环境变量（`ARCHIVE_*`），并额外通过
`apply_to_app()` 注入 `app.config` —— 因为它的任务是经 `celery.conf.app_config`
读取的（§5.2（3））。
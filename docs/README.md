# Moeflow 后端文档索引

本目录集中存放 Moeflow 项目的开发与设计文档。历史两份根目录文档
（`DEVELOPMENT_STATUS.md`、`MOEFLOW_PERFORMANCE_PROGRESS.md`）已迁入本目录，
便于统一维护和随仓库追溯。

## 当前环境摘要（2026-08-27）

- 后端运行基线为系统 Python 3.12.5，运行时和开发依赖分别由
  `requirements.txt`、`requirements-dev.txt` 锁定；直接依赖维护在对应的 `.in` 文件。
- Flask-APIKit 已移除，现有 Flask + Marshmallow API 能力由 `app/core/api.py` 提供。
- 开发服务器约有 960 MiB 内存和 1 GiB swap，后端与两个 Celery worker 每个容器上限
  512 MiB。完整测试必须关闭 coverage、禁用 xdist 并按测试目录分组串行执行。
- 当前开发服务器 release 为 `moeflow-20260827-214208-1b8c76e1-3282a7f5`；
  `development-status.md` 记录当前部署、迁移和测试结果，其他文档中的旧 release
  仅作为历史记录。

## 文档清单

| 文档 | 主题 | 维护方 |
| --- | --- | --- |
| `development-status.md` | 开发环境、依赖、分支状态、当前进度、已知问题与建议下一步（原根目录 `DEVELOPMENT_STATUS.md`） | 开发 |
| `moeflow-performance-progress.md` | 项目列表/团队列表/团队成员端点性能优化进度、实测数据、部署记录（原根目录 `MOEFLOW_PERFORMANCE_PROGRESS.md`） | 性能优化 |
| `identity-refactor-review-and-fixes.md` | 身份标签重构的代码审查结论、逐项修复、逐轮部署与验证记录（**§15 记录团队成员列表 N+1 优化 + 前端快捷编辑请求合并**） | 身份重构 |
| `project-identity-tags-change-summary.md` | 身份标签/团队成员/项目成员的对外 API 规格与设计决策（契约） | 身份重构 |
| `models.md` | MongoDB 持久化模型清单 | 数据模型 |
| `user_stories.md` | 用户故事 | 需求 |
| `archive-import-from-gallery-url.md` | 归档导入（来自图库 URL）的设计与契约 | 归档导入 |

## 迁移说明（2026-08-26）

- `DEVELOPMENT_STATUS.md` → `development-status.md`
- `MOEFLOW_PERFORMANCE_PROGRESS.md` → `moeflow-performance-progress.md`

两份文档已迁移至此，原根目录不再保留。文档内交叉引用已按同目录相对路径同步修正。

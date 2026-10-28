# 存储池（可选模块）

把**久未使用**的项目从站点默认存储搬到另一个 S3 兼容桶里，腾出默认桶的空间。
用户视角无感：项目照常打开，文件照常显示。

## 启用

目录存在即启用（没有开关，见 `optional-modules.md` §C1）：

```
app/modules/storage_pool/           # 后端：模型、API、迁移状态机
src/modules/storage_pool/           # 前端：管理页（菜单「存储池」）
```

部署侧不需要新容器或新队列：两个任务都走 `default` 队列，
日志里能看到 `Scheduled job storage-pool-scan`（每日 03:00）。

## 开关：自动巡查随时可关

装上模块后**默认不会自动搬任何东西**（`scan_enabled = false`）。第一次想搬，去后台点「立即巡查一次」，看着它跑完；确认没问题再把「每日自动挑选冷项目」打开。

实现上有个反直觉的决定：**beat 条目常驻，闸门在任务体里**。

```
beat（每日 03:00，永远在）
 └─ tasks.storage_pool_scan
      ├─ 读 StoragePoolSetting（读不到就按「关」处理）
      ├─ 关着 → return "跳过：自动巡查已停用"
      └─ 开着 → 挑候选、建任务、入队
```

原因：beat 是独立进程，运行时改 `celery.conf.beat_schedule` 只改当前进程的内存，它根本看不到——那样改完还得重启 beat，等于没有热切换。代价是每天空跑一次（读一条文档就返回），可忽略。

两个开关：

| 开关 | 默认 | 作用 |
|---|---|---|
| `scan_enabled` | **关** | 总闸。关掉后不再自动挑选新项目。**已在跑的迁移继续跑完**（停在中途会让项目长时间卡在「两处都有数据」的中间态；删源本来只在校验+切指针之后才做） |
| `run_enabled` | 开 | 紧急停止。关掉后 worker 不再领取新批次，已排队任务停在 PENDING，恢复后从断点续跑 |

读开关失败（Mongo 异常、文档损坏）一律按「关」处理：迁移会搬数据并删源，看不清就不动手。

`last_scan_*` 只在**真的执行了巡查**时更新；被开关跳过时不覆盖，免得把上一次的真实结果冲成「跳过」。

## 它到底做了什么

```
Project.storage_source_id = null      → 默认存储（全局 oss 单例）
Project.storage_source_id = <来源 id>  → 存储池里的那个来源
```

指针**同时决定读写**：切了指针之后，新上传的原图直接写进池来源，
天然不会出现「新文件在默认存储、老文件在池里」的分叉。

搬什么、不搬什么（见规划 §3.5）：

| 搬 | 不搬 |
|---|---|
| 原图 `files/<save_name>`（IMAGE） | 派生图 `cover-*` / `resample-*` / `proofread-*` |
| 文本原文 `files/<save_name>`（TEXT） | 头像 |
| 导出产物 `outputs/<id>/<file_name>` | |

派生图永远留在默认存储 —— `File._processed_image_url` 里显式写死走全局 `oss`，
误路由会让全站缩略图 404。

## 状态机（顺序即安全性）

```
PENDING → COPYING → VERIFIED → SWITCHED → DONE
```

- **切指针在删源之前**：切换前读源、切换后读池，中间没有「两边都读不到」的窗口。
- **切指针前重读 `project.edit_time`**：搬运期间有人动了文件就 ABORTED，宁可多中止几轮。
- **切指针在项目级锁内完成**（`storage_pool_lock`，租约 30 分钟，worker 被 kill 也不会永久卡住），
  锁内再枚举一次清单，确认没有新增对象。
- 每个对象的状态落在 `storage_transfer_object`，失败重试即从断点续跑。

单轮上限 `max_objects_per_run`（默认 2000），跑不完就留在 COPYING 等下一轮。

## 子路径（`sub_path`）是硬约束

`sub_path` **就是**该来源的 key 总前缀。默认 `storage/` 与站点默认存储一致，
所以迁移时对象 key 一字不改，只换桶。

**已有项目迁入后（`has_data = true`）禁止修改 `sub_path`** —— 改了等于让那些项目的
文件全部失联。前端置灰 + 后端 409 双拦，提示先回迁再改。

副产品：同一个桶可以用不同 `sub_path` 添加多次，等于在一个桶里开多个命名空间，
不需要多套凭据。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/v1/admin/storage-pool/sources` | 列表／新增（新增会先测连通性） |
| PUT / DELETE | `/v1/admin/storage-pool/sources/<id>` | 更新（`has_data` 时禁改 `sub_path`）／删除（有项目指向则拒绝） |
| POST | `/v1/admin/storage-pool/sources/<id>/test` | head_bucket + 写读删探针（探针用后即删） |
| GET | `/v1/admin/storage-pool/transfers` | 迁移记录与进度 |
| POST | `/v1/admin/storage-pool/projects/<id>/migrate` | 手动迁入（body 带 `source_id`） |
| POST | `/v1/admin/storage-pool/projects/<id>/restore` | 回迁到默认存储 |
| POST | `/v1/admin/storage-pool/transfers/<id>/retry` | 重试（失败对象回到待搬运） |
| GET / PUT | `/v1/admin/storage-pool/settings` | 读取／修改运行开关（只改传入的字段） |
| POST | `/v1/admin/storage-pool/scan` | **手动巡查一次，不受开关限制**（关掉自动、偶尔手动的用法） |
| GET | `/v1/admin/storage-pool/stats` | 统计 |

## 部署后看不到「存储池」菜单？

菜单是**无条件渲染**的（没有管理员判断），`src/modules/storage_pool/index.ts` 存在就会有。看不到基本只有一种原因：**你访问的那一层还在发旧的 JS 包**。

排查顺序：

```bash
# 1. 源站（前端容器）发的是哪个包
docker exec moeflow-deploy-moeflow-frontend-1 sh -lc \
  'grep -o "index-[A-Za-z0-9_-]*\.js" /build/index.html'

# 2. 公网实际拿到的又是哪个包
curl -s https://<你的域名>/ | grep -o 'index-[A-Za-z0-9_-]*\.js'

# 3. 新包里到底有没有模块（命中 ≥1 说明代码没问题）
docker exec moeflow-deploy-moeflow-frontend-1 sh -lc \
  'grep -c "storage-pool" /build/assets/index-<hash>.js'
```

两个 hash 不一致，就是中间那层（另一台 nginx / CDN / 挂了 proxy_cache 的反代）没刷新：同步 `build/` 或清缓存后 reload。

另外两个容易误判的点：

- **手机版不显示模块菜单**：`DashboardMenu` 里 `moduleMenuItems` 整体包在 `{!isMobile && ...}` 中，窗口很窄或真机访问时看不到「存储池」是设计如此，不是没装上。直接访问 `/dashboard/storage-pool` 仍可打开。
- **能看到页面但一直报加载失败**：多半是当前账号不是管理员。所有存储池接口都是 `@admin_required`，非管理员会拿到 403/无权限。


> 2026-10-26 实测：本机前端容器已发 `index-Dj8U8hYe.js`（含模块，命中 2 次），但公网 `shoreline.akiusagi.cc` 仍返回 `index-Dxzq23_f.js`（命中 0 次），且响应头是 `server: nginx/1.18.0` —— 而本机容器是 nginx 1.26.3、只监听 5080/50443。说明公网 443 由另一台 nginx 承接，它手上是旧副本。

## 安全与已知风险

- **凭据明文入库**（站长决定）：拿到 Mongo 读权限即握有池内桶的读写凭据。
  接口侧绝不返回 `secret_access_key`，`access_key_id` 只回前 4 后 4。
- **单副本策略**：搬运成功后删源端对象。校验不严就是永久丢失，
  因此只有 VERIFIED 的对象才会被删。
- **跨服务商**：同 endpoint 走服务端复制，否则下载→上传（流式，不整块进内存）。
  R2 不支持跨桶 CopyObject，这类情况会自动退回流式。
- 项目复活后新文件写进池来源；要搬回默认存储请手动「回迁」，不做自动回迁。

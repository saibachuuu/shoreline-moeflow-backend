# 外部项目查询模块

实现位于 `app/modules/partner_search/`，通过既有模块扫描机制注册，不再由核心 `app/apis/urls.py` 硬编码。

- 查询：`POST /v1/partner-search-query-entry`，原地址、请求/响应结构、团队范围、启用状态、限流和缩略图选择规则不变；支持 OPTIONS。
- 管理：`GET/PUT /v1/partner-search-query-entry/settings`，沿用 `admin_required` 与原字段校验；PUT 支持部分更新。
- 配置字段：`partner_search_enabled`、`partner_search_team_ids`、`partner_search_rate_limit_seconds`、`partner_search_max_limit`。
- 原核心 `/v1/admin/site-setting` 不再返回或管理这些字段。请配套更新前端，其模块自有表单使用新接口独立保存。

配置保留：模块通过独立的 MongoEngine 文档视图读写原 `site_setting` 集合中的 `pse/pst/psq/psm` 字段，只更新自身字段，不需要迁移、不重置已有设置。核心设置模型以 `strict=False` 忽略可选字段，因此移除模块后仍能读取和保存原设置，模块字段也不会丢失。限流集合名称仍为 `partner_search_throttle`。

删除 `app/modules/partner_search/` 即不注册两个接口；核心不含任何对此模块的导入。模块专属测试在未安装模块时跳过，通用接线测试继续运行。

CDN 切换此次为前端模块：后端主分支没有新增的客户端线路切换代码；原有通用 OSS/CDN 签名存储服务不属于此次新增功能，不复制或迁出。

验证：
```sh
python -m pytest tests/api/test_partner_search_api.py tests/api/test_site_setting_api.py tests/base/test_modules_registry.py tests/base/test_modules_wiring.py -o addopts= -q
```

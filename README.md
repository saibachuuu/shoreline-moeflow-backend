# moeflow-backend

API + storage server for [moeflow](https://github.com/moeflow-com)

[![codecov](https://codecov.io/gh/moeflow-com/moeflow-backend/graph/badge.svg?token=LQJBLB495F)](https://codecov.io/gh/moeflow-com/moeflow-backend) [![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=moeflow-com_moeflow-backend&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=moeflow-com_moeflow-backend)

## How to deploy

Please refer to instruction in https://github.com/moeflow-com/moeflow-deploy

## How to develop


## model

## 版本更新内容 (Changelog)

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

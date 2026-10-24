# moeflow-backend

API + storage server for [moeflow](https://github.com/moeflow-com)

[![Chinese README](https://img.shields.io/badge/README-中文-red)](README_zh.md)
[![English README](https://img.shields.io/badge/README-English-blue)](README.md)

[![codecov](https://codecov.io/gh/moeflow-com/moeflow-backend/graph/badge.svg?token=LQJBLB495F)](https://codecov.io/gh/moeflow-com/moeflow-backend) [![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=moeflow-com_moeflow-backend&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=moeflow-com_moeflow-backend)

This version adds notification system, proofread tips, and position lock APIs. **Please use it with MoeFlow frontend Version 1.2.6 or later** for full functionality.

This version requires an **S3-compatible object storage** (e.g. Aliyun OSS, Cloudflare R2) for file storage. For other storage backends, see the following community branch:

- **Local disk storage** [`scanlation/moetran-local-backend`](https://github.com/scanlation/moetran-local-backend)

## How to deploy

Please refer to instructions in https://github.com/moeflow-com/moeflow-deploy

## How to develop

1. Install Python 3.10
2. Service dependencies: MongoDB, Erlang, RabbitMQ
3. `pip install -r requirements.txt` (on Windows, set the `PYTHONUTF8=1` environment variable if you hit errors)
4. Create `/configs/dev.py` based on the `/config.py` template for development (this directory is git-ignored)
5. During development, edit the required settings directly in `/configs/dev.py`
6. Set the environment variable `CONFIG_PATH=../configs/dev.py` before running
7. Run the main process: `python manage.py run`
8. With `DEBUG` enabled, verification codes and similar messages are printed directly to the console log
9. _(Optional)_ Import and export features require two celery worker processes; see the Celery section below

## Configure Celery

1. To run a Celery worker on Windows, install `eventlet` first and pass the pool option, otherwise you will see: `not enough values to unpack (expected 3, got 0)`
2. _(Optional)_ Install `eventlet` on Windows: `pip install eventlet`
3. The two workers need two terminals (**PowerShell example**); set `CONFIG_PATH=../configs/dev.py` before running
4. Start the main Celery worker (email sending, term analysis): `celery -A app.celery worker -n default -P eventlet --loglevel=info`
5. Start the output Celery worker (project import, thumbnail generation, translation export, project export): `celery -A app.celery worker -Q output -n output -P eventlet --loglevel=info`
6. On non-Windows platforms, drop the `-P eventlet` part from the commands if you see errors

## How to test

1. Configure `test.py`:
   1. `DEBUG = True` and `TESTING = True`
   2. Use the `mongomock://` scheme for `DB_URI` and end the database name with `_test`
   3. Set `CONFIRM_EMAIL_WAIT_SECONDS`, `RESET_EMAIL_WAIT_SECONDS` and `RESET_PASSWORD_WAIT_SECONDS` to `1` to avoid excessive waiting
2. Run `export CONFIG_PATH=/path/to/configs/test.py && pytest -n auto` for parallel testing

## Changelog

### Version 1.2.0 (Shoreline Custom Edition)

#### Core & Full-Stack Features
- **Independent Preview Image Compression**: Resampled large images and small thumbnails are generated asynchronously and independently, optimizing storage and concurrency, and significantly improving image loading and transfer performance.
- **Quick Staff Assignment on Project Cards**: Register and update project workers directly from project list cards, keeping staff assignments in sync with the frontend in real time.
- **User Aliases & Identity System**: Personal global alias maintenance, team-creator overrides, and display-name preference configuration.
- **Real-Time Project List Polling & Presence**: Presence heartbeat mechanism and dynamic activity APIs, perceiving others' editing state at millisecond level and broadcasting list changes.
- **Search Projects by Member / Across Project Sets**: Combined filtering across teams and project sets by specific member and role (translator, typesetter, proofreader, raw provider, etc.).
- **Send Proofread Draft via Email**: Professional proofread feedback emails to translators with typeset image-text diffs, embedded thumbnails on the left side, and multi-recipient CC (strictly RFC 2046 compliant).
- **Import Comics from TG Bot**: Asynchronous archive import API (Archive Import) for external bots such as Telegram to auto-unpack archives and create translation projects in one click.

#### Backend-Exclusive Features & Infrastructure
- **Auto-inserted Staff Credits Page on Export**: When exporting a complete work, a staff credits page is automatically typeset from registered project members and inserted at a designated position (first page, page 2, or last page).
- **Public Project Query API for Partners**: Anti-collision and progress-sync query APIs (partner project collision search), allowing partner teams and external bots to safely check project existence and progress.
- **Cloudflare R2 / S3-Compatible Object Storage**: Native `boto3` / `botocore` integration with seamless upload, signing, and streaming support for Cloudflare R2 and S3-compatible storage.
- **Versioned Database Migration Framework**: Automated version migration mechanism built on `manage.py migrate`, ensuring lossless and smooth database schema upgrades.
- **Lightweight Performance & Index Optimization**: Rebuilt composite indexes and projection queries for large tables, greatly reducing loading latency for large manga project lists.

### Version 1.2.1

- **Admin Tag Assignment Fix in Member Changes**: Project admins and team admins are now allowed to assign the admin tag in member change operations.

### Version 1.2.2

- **Direct Self-Assignment**: Adding yourself to a project position directly activates the member record instead of creating a self-invitation.
- **Open Qualification Mode Recruiting**: In open qualification mode, ordinary members can assign and update worker tags for others without being blocked by the admin permission gate.
- **500 Fix for Missing Tags**: Added `_LegacyTagsField` and migration m0007, treating missing/null tags as empty lists to prevent a TypeError 500 when opening project sets containing members with tags.

### Version 1.2.3

- **Auto-merge When Binding External Aliases**: When the target registered user already has a project member record, `bind()` automatically merges the external alias into it (union of position tags, external display name adopted, external record hard-deleted) instead of raising `MEMBER_MERGE_REQUIRED`.
- **Removed External Members Can Bind**: External members in the soft-deleted (removed) state can bind a registered user and are revived as active registered members.
- **Hard Delete for External Aliases**: New `hard_delete_external()`; the project owner or team creator can permanently delete an external alias and free its `(project, external_id)` unique slot; new `DELETE /v1/projects/<id>/members/<member_id>` endpoint.
- **Error Code Enhancement**: `APIError.to_tuple()` attaches a stable `identity_code` string alongside the numeric code, enabling precise frontend fallbacks in concurrent-merge scenarios.
- **Environment & CI Fixes**: Restored the Python 3.12 runtime (matching database migration checksums) and synced uv-generated dependencies; replaced the removed `TimedJSONWebSignatureSerializer` with `jwt.decode` in `user.py`; fixed the unhashable type error triggered by marshmallow 3.26 `ValidationError(msg, [field_name])`; CI now installs `requirements-dev.txt` for pytest; ruff formatting cleanup.
- **Tests**: Updated bind cases to expect auto-merge; added owner-merge, hard-delete, and removed-external-bind cases; migration 0007 added to the expected migration list.

### Version 1.2.4

- **Optional Backend Module System**: Module-based loading mechanism (a directory under `modules/` acts as the switch) enabling plug-and-play optional features with strict decoupling.
- **Modularized Business Features**: Archive import (`archive_import`) and partner catalogue search (`ziteng_partner` collision query and status APIs) refactored into standalone modules.
- **Developer Documentation & Tooling**: Cleaned up legacy Makefile workflows and standardized development and maintenance scripts.

### Version 1.2.5

- **Custom Project File Ordering & Reset**: Persistent custom ordering of project files (`manual_order`), adjustable by project or team managers only; snapshot version conflict checks ensure concurrency safety.
- **Export & Navigation Consistency**: Pagination navigation, LabelPlus export, and numbered image archive exports all follow the custom ordering.
- **Reset to Default & Automated Migration**: One-click clearing of the custom order back to the default natural order; new `m0008_file_manual_order` migration script for index and field evolution.

### Version 1.2.6

- **Core Notification System**: New core notification service and inbox, independent of optional modules, unifying in-site notifications and email delivery. The system/team/project/personal contracts are preserved: system notifications can only be sent by valid site admins; team creators may delegate team notifications to team admins and configure whether project notifications include project managers. Recipients can be filtered by everyone, management roles, worker qualifications or positions, and specific members; content supports BBCode. Site admins can browse, search, and withdraw messages with a second confirmation. Delivery uses reliable background dispatch with conditional sync to reduce polling latency.
- **Proofread Tips (Annotations) API**: New `POST /v1/sources/<source_id>/tips` to create a tip, plus endpoints to edit and delete your own tips (proofread permission required); each proofreader keeps at most one tip per source and target language — unique index with overwrite semantics, falling back to updating the stored record on concurrent-write conflicts.
- **Rich Proofread Draft Email & Background Delivery**: The draft email body now renders per-label tips (with author and time), and pages containing tips only (without translation changes) are included. Position markers are baked into per-page image copies with Pillow (pink numbers inside frames / yellow outside, cached by label fingerprint, degrading to the original image on failure), avoiding Gmail stripping positioned styles. Mobile clients get a stacked image-above-labels layout with `#N` / proofread / tip badge prefixes on label rows. Baking moved off the request path: a Celery task bakes, renders, and delivers in the background while the sender tracks progress through an inbox receipt; the legacy direct-send path keeps synchronous behaviour. All template copy is localized via gettext (new Jinja2 babel mapping) and rendered under an explicit locale.
- **Two-Level Project Position Locks**: Projects gain `position_lock_enabled` / `position_lock_tags` fields (writable by the team creator only; the tag list is cleared when the master switch turns off). The level-1 master switch locks the admin tag and all team custom tags; level-2 optionally locks selected worker roles among the seven worker tags. Enforcement lands at the shared tag-validation choke point (add/update/merge/bind/invitation paths); removing or restoring members holding locked tags is also team-creator-only, and the application-approval join path is covered as well. New tests cover settings permission, both lock levels, removal locking, unlock restoring edits, and invitation rejection.
- **Site Administrator Restriction**: Assigning the site administrator privilege is restricted to the configured site owner.
- **Partner Search Module Isolation**: Partner catalogue search APIs and settings moved out of core code into the `partner_search` optional module.

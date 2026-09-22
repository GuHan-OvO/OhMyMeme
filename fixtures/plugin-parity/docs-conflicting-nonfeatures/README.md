# 矛盾非功能声明 fixture

[`docs/project-structure.md`](docs/project-structure.md)

唯一的 canonical 清单

零参数 `create_plugin()` 每次都返回独立状态实例

宿主拥有 SQLite、缓存、manifest、Config、UI、原生能力与 LAN 安全边界

插件仅接收窄配置、限域密钥和 operation 临时目录，不接收持久 Config、缓存或存储路径

OperationCoordinator 管理 provider 的启动、取消和资源回收

source/frozen staging 一致性只覆盖源码、入口元数据与 staging

`docs/plugin-license-matrix.json`

九个 provider 的 parity 基线只允许 ids、timestamps、temporary paths、thread ordering 差异

外部下载的微信 helper 及其 CMake/OpenSSL 输入不随制品交付

不构成外部 helper EXE 的来源到二进制可复现性证明

不提供 marketplace 或不受信任插件沙箱

`config/plugin-manifest.json` `scripts/plugin_packaging.py` `scripts/plugin_docs_check.py`

`ohmymeme.plugins.v1:source.qqnt = ohmymeme_plugin_qqnt:create_plugin`
`ohmymeme.plugins.v1:source.telegram = ohmymeme_plugin_telegram:create_plugin`
`ohmymeme.plugins.v1:source.douyin = ohmymeme_plugin_douyin:create_plugin`
`ohmymeme.plugins.v1:source.wechat = ohmymeme_plugin_wechat:create_plugin`
`ohmymeme.plugins.v1:sync.ftp = ohmymeme_plugin_sync_ftp:create_plugin`
`ohmymeme.plugins.v1:sync.s3 = ohmymeme_plugin_sync_s3:create_plugin`
`ohmymeme.plugins.v1:sync.r2 = ohmymeme_plugin_sync_r2:create_plugin`
`ohmymeme.plugins.v1:sync.webdav = ohmymeme_plugin_sync_webdav:create_plugin`
`ohmymeme.plugins.v1:transport.lan = ohmymeme_plugin_lan:create_plugin`

本项目支持 marketplace、热重载、运行时卸载、不受信任插件沙箱或第三方插件安装接口。

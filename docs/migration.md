# 2.x 升级指南

[返回 README](../README.md) · [使用指南](usage.md) · [元数据说明](metadata.md) · [升级指南](migration.md)

## V2.0 起的重大变化

V2.0 是处理流程的重构起点。下表区分 2.0 的基础改造与后续 2.x 的演进，避免将当前所有功能都归到 2.0.0。

| 阶段 | 变化 | 对使用者的影响 |
| --- | --- | --- |
| 2.0.0 | 扫描、抓取、合并、规划、提交拆分为独立组件，统一单片及批次结果 | 路径冲突提前检查，失败阶段和未处理任务可追踪 |
| 2.0.0 | 影片、字幕和元数据纳入输出事务 | 先准备校验，再提交；异常时尝试恢复，恢复不完整则停止批次并保留材料 |
| 后续 2.x | Docker 无交互批处理、本地模型翻译、分片续传和有界影片并发 | 持久化配置与模型目录，预留首次资源下载及模型运行空间 |
| 后续 2.x | 演员映射、输出命名与源目录清理持续调整 | 新生成的目录和演员名称可能变化；运行前核对清理范围 |
| 2.1.0 | 移除在线多站点刮削，R18.dev 本地库成为唯一元数据来源 | 未收录影片不再回退其他站点；旧抓取配置需手动迁移 |

逐版本记录见[更新日志](../CHANGELOG.md)，当前行为以[使用指南](usage.md)和[元数据说明](metadata.md)为准。

## 升级步骤

1. 备份现有配置，保留媒体和运行数据；对照[默认配置](../config_default.yml)核对自定义项。
2. 保留 `/media`、`/app/config` 挂载，增加可写且持久化的 `/app/models`。数据库与演员映射也保存在 `/app/config`，不要在换镜像时删除。
3. 从 1.x 升级时，移除 `translator.engine`、`translator.fields`，改用 `translator.model`、`model_dir`、`runtime` 和 `generation`。翻译始终启用，`engine: null` 不能关闭它；模型准备失败会阻止扫描。
4. 按下表迁移旧抓取规则，核对自定义输出模板。产物目标重叠或固定路径无法满足长度限制时，会在写入前失败。
5. 阅读[源目录清理与恢复规则](usage.md#失败处理与日志)。默认移动模式会清理符合条件的残留，包含过小或无法识别的视频；正常空扫描也可能清理历史残留。
6. 选择已发布的固定版本标签，先用少量影片副本确认目录、NFO 和演员名称，再处理整个目录。`latest` 随稳定版发布变化，`dev` 用于开发构建。

已整理影片不会自动批量改名或改写 NFO；媒体中心需重新读取更新后的元数据。不要将更新镜像当作历史媒体库迁移。

## 元数据配置迁移

| 旧键 | 当前处理方式 |
| --- | --- |
| `crawler.required_keys` | 手动迁至 `r18dev.required_keys` |
| `crawler.normalize_actress_name` | 手动迁至 `r18dev.normalize_actress_name` |
| `crawler.respect_site_avid` | 手动迁至 `r18dev.respect_source_id` |
| 其余 `crawler` 配置、`r18dev.enabled`、`network.proxy_free`、`concurrency.crawlers` | 删除；在线站点及本地库启停开关已移除 |

旧键会被忽略并提示，**不会自动继承旧值**；用户文件、环境变量和 CLI 覆盖均如此。

```yaml
r18dev:
  required_keys: [cover, title]
  normalize_actress_name: true
  respect_source_id: true
```

本地 Python 环境仅用于开发和测试。内部接口迁移及安装元数据刷新见[开发指南](development.md)。

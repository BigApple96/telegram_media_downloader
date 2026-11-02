# config.yaml 自动监听视频下载

在 `config.yaml` 指定频道和起始消息，容器启动后持续下载视频，并自动把进度写回同一个配置文件。沿用 `/listen_forward` 的定期读取消息方式和原有下载队列，无需中转群、监听命令或群晖计划任务。已有 Bot 功能可以继续使用；监听下载使用已登录的 Telegram 用户会话。

## 配置频道和起点

保留现有 API 凭据、代理、下载目录等配置，添加以下顶层配置块：

```yaml
video_monitor:
  enabled: true
  interval: 60
  chats:
    - chat_id: -1004342657661
      start_message_id: 130
```

`start_message_id` **包含该条消息**。之前提供的链接为 `https://t.me/c/4342657661/129`：如果要跳过 129 及之前的内容，填 `130`；如果连 129 也要处理，填 `129`。起点是固定消息编号，程序不会在每次启动时跳到最新消息。

处理到消息 135 后，程序自动将该配置变为：

```yaml
video_monitor:
  enabled: true
  interval: 60
  chats:
    - chat_id: -1004342657661
      start_message_id: 130
      last_read_message_id: 135
```

此时重启从 **136** 开始，`last_read_message_id` 优先于 `start_message_id`。首次不用填写进度字段，之后不要用旧配置覆盖它。

| 字段 | 含义 |
| --- | --- |
| `enabled` | `true` 开启；`false` 恢复原来的批量下载模式。未配置此段或 `chats: []` 时也使用原模式。 |
| `interval` | 每轮读取和下载结束后等待的秒数，默认 60，最小 10。积压超过 100 条时继续翻页，不等待下一轮。 |
| `chat_id` | 频道数字 ID、公开用户名或 `t.me` 消息链接。推荐数字 ID；邀请链接不支持，账号必须已加入并有权限读取。链接中的消息编号不会自动作为起点，请填写下一个字段。 |
| `start_message_id` | 首次从哪条开始，包含该条；必须是大于等于 1 的整数。每个频道分别填写。 |
| `last_read_message_id` | 程序自动保存的最后连续处理成功的消息编号。视频需下载成功或已完整存在；非视频及配置排除的格式会跳过并推进进度。 |

添加其他频道时，在 `chats` 中继续添加条目，每个频道都有独立起点和进度。修改配置前先停容器，再修改并启动，避免运行中的进度写回覆盖手动修改。若确实要更改一个已有频道的起点，停容器后修改 `start_message_id` 并删除该频道的 `last_read_message_id`。

## 群晖挂载配置文件夹

程序使用“写临时文件、刷新到磁盘、原子替换”的方式保存进度，避免直接截断配置文件。Docker 必须挂载**配置文件所在的文件夹**，不能只把单个 `config.yaml` 绑定到 `/app/config.yaml`，也不能设为只读。

建议目录：

```text
telegram-downloader/
  compose.monitor.yaml
  settings/
    config.yaml
  data.yaml
  sessions/
  downloads/
  temp/
  log/
```

完整示例见根目录 `compose.monitor.yaml`。其中与配置相关的部分为：

```yaml
command: ["python", "media_downloader.py", "--config", "/app/settings/config.yaml"]
volumes:
  - ./settings:/app/settings
```

这只指定配置路径；频道、起点、检查间隔都放在 `settings/config.yaml`。不再需要 `TDL_MONITOR_*` 环境变量或额外的 `state/monitor.json`。

升级步骤：

1. 停止旧容器，备份原配置、`data.yaml`、`sessions` 和下载目录，避免两个容器共用同一会话。
2. 创建 `settings` 文件夹，将现有 `config.yaml` 放入其中，添加上面的 `video_monitor` 配置。不要用带占位凭据的示例覆盖自己的配置。
3. 按新的 Compose 示例挂载 `settings` 文件夹，保留其他原有挂载。群晖也可以使用 `/volume1/docker/你的目录/settings:/app/settings` 这样的绝对路径。
4. 使用包含本次代码的镜像启动；上游镜像不识别这个新增配置。
5. 查看 `Video monitor initialized ... after_message=129` 日志，确认起点。下载一个起点之后的视频后，检查 `settings/config.yaml` 中的进度；重启后应从保存的进度之后继续。

未登录的全新安装仍需先交互登录，已有有效会话可沿用。

## 构建和启动

从本地修改后的完整源码构建：

```sh
TDL_IMAGE=telegram-media-downloader-monitor:local docker compose -f compose.monitor.yaml up -d --build
docker compose -f compose.monitor.yaml logs -f --tail=100
```

推送修改到 fork 的 `master` 后，GitHub Actions 的 `Test and publish Docker image` 工作流会测试并发布 `ghcr.io/bigapple96/telegram_media_downloader:latest`，提供 `linux/amd64` 和 `linux/arm64`；也支持手动运行。32 位 ARM 不在此构建范围内。首次发布的 GHCR 包如果是私有，需要登录 GHCR，或由仓库所有者将包设为公开。

确认镜像实际发布成功后，才能使用：

```sh
docker compose -f compose.monitor.yaml pull
docker compose -f compose.monitor.yaml up -d
```

工作流配置存在不代表镜像已发布。

## 下载及恢复行为

- 识别普通 `video` 和 MIME 为 `video/*` 的 `document`。不会下载文本、照片、普通文档或 GIF；不会只凭扩展名把 `application/octet-stream` 附件认作视频。
- 监听模式独立于全局 `media_types`，仍遵守 `file_formats.video` / `file_formats.document`；这两个格式配置缺失时使用 `all`。
- 监听启用后，不执行顶层 `chat` 的启动批量扫描，避免意外下载其他历史内容。已有机器人手动命令仍可使用。
- 使用原有 `max_download_task` 并发数和下载进度页面。输出目录为 `save_path/monitor/频道ID/...原有子目录...`。
- 每个频道按消息顺序保存连续完成的位置。某个视频失败时，不会越过它提交进度；其他频道可以继续下载。
- **每条消息处理成功后立即写回进度**，无需等下一次关机。正在下载、失败或取消的消息不会被记为完成。非视频也会推进位置，所以这个编号表示最后成功处理的消息，不一定是最后一个视频。
- 正常重启不会重新调度进度之前的消息。若恰好在文件落盘后、进度保存前被强制中断，下次会再次检查该条；同路径且大小完整的本地文件会跳过，不会重新传输。不要在运行中删除下载目录或覆盖进度。
- 断网或停机后会补处理起点/进度之后仍可读取的消息。已删除、失去权限的消息以及后来编辑的旧消息无法保证补回。
- 如果原配置开启云盘上传和上传后删除本地文件，监听也沿用这些设置；需要保留 NAS 文件时请检查已有选项。删除后的文件在上述异常中断情形下可能需要重新下载。
- 配置损坏、起点非法或无法写回时会报错，不会悄悄重置进度。配置注释和无关字段会保留。

## 离线验证

安装 `requirements.txt` 后运行：

```sh
python -m unittest tests.test_video_monitor tests.test_video_monitor_integration -v
```

测试使用合成消息和临时目录，验证指定起点、进度写回、重启恢复、失败重试及真实下载队列接入；不代表已经在真实频道或群晖完成验收。

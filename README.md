# Telegram 私聊自动清理

AstrBot 插件。只处理指定 Telegram 适配器与单个私聊。用户的新消息和机器人的回复从各自发送时刻开始计时，默认保留 3600 秒。检查间隔约 5 秒。复用 AstrBot 已配置的 Telegram Bot，无需再次填写 Token。

## 安装

1. 在 AstrBot 管理面板打开 **插件 → 已安装 → 安装插件 → 从文件安装**，上传 `astrbot_plugin_telegram_private_ttl.zip`，无需解压。
2. 在目标 Telegram 私聊向机器人发送 `/sid`。记下 **UID** 和 **Bot ID**；也可在 AstrBot 的「机器人」页查看 Telegram 适配器的 ID。
3. 打开「Telegram 私聊自动清理」插件的配置。填入平台 ID（Bot ID）和私聊 ID（UID），将保留时间设为 `30`，打开「启用自动清理」并保存。
4. 在 1Panel 中重启 AstrBot，或者在服务器运行 `docker restart astrbot`。
5. 给机器人发一条可以删除的测试文本。等到两条消息分别发出约 30 秒后，确认用户消息和机器人回复都消失。
6. 把保留时间改为 `3600`，保存后重启 AstrBot。停用时，关闭插件配置中的「启用自动清理」并重启，或在插件管理页禁用该插件。

## 行为与边界

- 插件默认关闭，且必须指定私聊 ID。只处理启用后观察到的新消息，不扫描旧聊天记录。Telegram 骰子和聊天设置通知等服务消息不在清理范围内。
- 删除队列保存在 AstrBot 数据目录 `plugin_data/astrbot_plugin_telegram_private_ttl/pending.sqlite3`，只包含机器人 ID、会话 ID、消息 ID 和时间。重启可继续清理已登记的消息，已超过 Telegram 48 小时窗口的消息会跳过。程序离线期间尚未登记的消息不保证覆盖。
- 延时按消息登记时的配置计算。改时间只影响新登记的消息。重新启用同一个私聊时，插件可继续处理之前登记的未过期任务。
- 删除范围是 Telegram 聊天窗口。AstrBot 的上下文、聊天记录、日志和模型服务方保存的数据按各自设置处理。
- 插件通过 Python Telegram Bot 的 `ExtBot._do_post` 观察已成功的 Telegram 调用，不修改 AstrBot 主程序文件。实现参考了 AstrBot v4.22.1 与当前主分支的 Telegram 适配器，升级 AstrBot 或 Telegram SDK 后请重新做双向测试。

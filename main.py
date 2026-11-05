import asyncio
import sqlite3
import time
from functools import wraps

from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import ExtBot


PLUGIN_NAME = "astrbot_plugin_telegram_private_ttl"
DELETE_WINDOW = 48 * 60 * 60
POLL_SECONDS = 5
CONTENT_FIELDS = {
    "text", "animation", "audio", "contact", "document", "game", "location",
    "photo", "poll", "sticker", "venue", "video", "video_note", "voice",
    "checklist", "live_photo", "paid_media", "rich_message", "story",
}


class TelegramPrivateTTL(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.settings = config if config is not None else {}
        self.db = None
        self.worker = None
        self.previous_post = None
        self.wrapped_post = None
        self.accept_from = 0

    async def initialize(self):
        if not self.settings.get("enabled", False):
            return

        self.platform_id = str(self.settings.get("platform_id", "")).strip()
        try:
            self.chat_id = int(self.settings.get("private_chat_id", ""))
            self.delay = int(self.settings.get("ttl_seconds", 3600))
        except (ValueError, TypeError):
            logger.warning("[Telegram TTL] 私聊 ID 或保留秒数格式错误，未启用。")
            return
        if not self.platform_id or self.chat_id <= 0 or not 5 <= self.delay <= 86400:
            logger.warning("[Telegram TTL] 请设置适配器 ID、正数私聊 ID 和 5–86400 秒保留时间。")
            return

        previous = ExtBot._do_post
        if getattr(previous, "_astrbot_private_ttl", False):
            logger.warning("[Telegram TTL] 已有清理实例，跳过重复加载。")
            return

        path = StarTools.get_data_dir(PLUGIN_NAME) / "pending.sqlite3"
        self.db = sqlite3.connect(path)
        try:
            with self.db:
                self.db.execute("""
                    CREATE TABLE IF NOT EXISTS pending (
                        bot_id INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        next_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL,
                        PRIMARY KEY (bot_id, chat_id, message_id)
                    )
                """)
        except sqlite3.Error:
            self.db.close()
            self.db = None
            raise

        # Both updates and sent messages pass through this SDK method.
        # Preserve every request argument and return the original result.
        @wraps(previous)
        async def observe(bot, endpoint, *args, **kwargs):
            result = await previous(bot, endpoint, *args, **kwargs)
            if self.db is not None and (
                endpoint == "getUpdates" or endpoint.startswith("send")
            ):
                try:
                    if bot is self.target_bot():
                        self.record_result(bot.id, endpoint, result)
                except Exception as exc:
                    # Observing messages must never interrupt polling or replies.
                    logger.warning(f"[Telegram TTL] 无法登记消息：{type(exc).__name__}")
            return result

        observe._astrbot_private_ttl = True
        self.accept_from = int(time.time())
        self.previous_post = previous
        self.wrapped_post = observe
        ExtBot._do_post = observe
        self.worker = asyncio.create_task(self.cleanup_loop(), name=PLUGIN_NAME)
        logger.info(f"[Telegram TTL] 已启用，保留 {self.delay} 秒。")

    def target_bot(self):
        adapter = self.context.get_platform_inst(self.platform_id)
        if adapter is None or adapter.meta().name != "telegram":
            return None
        bot = adapter.get_client()
        return bot if isinstance(bot, ExtBot) else None

    def record_result(self, bot_id, endpoint, result):
        if endpoint == "getUpdates":
            if not isinstance(result, list):
                return
            messages = (update.get("message") for update in result if isinstance(update, dict))
        else:
            messages = result if isinstance(result, list) else (result,)

        rows = []
        for message in messages:
            if not isinstance(message, dict) or not CONTENT_FIELDS.intersection(message):
                continue
            chat = message.get("chat") or {}
            if chat.get("type") != "private" or chat.get("id") != self.chat_id:
                continue
            # Telegram applies a different deletion window to private dice messages.
            if "dice" in message:
                continue
            message_id = message.get("message_id")
            sent_at = message.get("date")
            if not isinstance(message_id, int) or message_id <= 0:
                continue
            if not isinstance(sent_at, int) or sent_at < self.accept_from:
                continue
            rows.append((bot_id, self.chat_id, message_id, sent_at + self.delay,
                         sent_at + DELETE_WINDOW))

        if rows:
            with self.db:
                self.db.executemany("INSERT OR IGNORE INTO pending VALUES (?, ?, ?, ?, ?)", rows)

    async def cleanup_loop(self):
        while True:
            try:
                await self.cleanup_due()
            except (sqlite3.Error, RuntimeError) as exc:
                logger.warning(f"[Telegram TTL] 清理暂缓：{type(exc).__name__}")
            await asyncio.sleep(POLL_SECONDS)

    async def cleanup_due(self):
        now = int(time.time())
        with self.db:
            self.db.execute("DELETE FROM pending WHERE expires_at <= ?", (now,))

        bot = self.target_bot()
        if bot is None:
            return
        try:
            bot_id = bot.id
        except RuntimeError:
            return  # The Telegram adapter has not completed startup yet.

        due = self.db.execute("""
            SELECT message_id, expires_at FROM pending
            WHERE bot_id = ? AND chat_id = ? AND next_at <= ?
            ORDER BY next_at LIMIT 20
        """, (bot_id, self.chat_id, now)).fetchall()
        for message_id, expires_at in due:
            if self.target_bot() is not bot:
                break  # The adapter was reloaded during this batch.
            key = (bot_id, self.chat_id, message_id)
            retry_at = None
            try:
                if time.time() < expires_at:
                    await bot.delete_message(
                        chat_id=self.chat_id, message_id=message_id,
                        connect_timeout=10, read_timeout=15,
                    )
            except RetryAfter as exc:
                wait = exc.retry_after
                if hasattr(wait, "total_seconds"):
                    wait = wait.total_seconds()
                retry_at = int(time.time()) + max(1, int(wait)) + 1
                with self.db:
                    self.db.execute("""
                        UPDATE pending SET next_at = MAX(next_at, ?)
                        WHERE bot_id = ? AND chat_id = ?
                    """, (retry_at, bot_id, self.chat_id))
                break
            except BadRequest as exc:
                if "not found" not in str(exc).lower():
                    logger.warning("[Telegram TTL] Telegram 拒绝删除一条消息，已跳过。")
            except Forbidden:
                logger.warning("[Telegram TTL] 机器人无权删除一条消息，已跳过。")
            except TelegramError as exc:
                retry_at = int(time.time()) + 60
                logger.warning(f"[Telegram TTL] 删除暂缓，稍后重试：{type(exc).__name__}")

            with self.db:
                if retry_at is not None and retry_at < expires_at:
                    self.db.execute("""
                        UPDATE pending SET next_at = ?
                        WHERE bot_id = ? AND chat_id = ? AND message_id = ?
                    """, (retry_at, *key))
                else:
                    self.db.execute("""
                        DELETE FROM pending WHERE bot_id = ? AND chat_id = ? AND message_id = ?
                    """, key)

    async def terminate(self):
        if self.wrapped_post is not None and ExtBot._do_post is self.wrapped_post:
            ExtBot._do_post = self.previous_post
        if self.worker is not None:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        if self.db is not None:
            self.db.close()
            self.db = None

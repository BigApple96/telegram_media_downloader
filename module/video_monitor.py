"""Persistent polling for automatic video downloads (no bot required)."""

import asyncio
import errno
import logging
import os
import re
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple, Union
from urllib.parse import urlparse

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)
ChatId = Union[int, str]


def parse_chat(value) -> ChatId:
    """Accept numeric IDs, public usernames and t.me message links."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("video_monitor chat_id must be an ID or username")
    text = str(value).strip()
    if text.startswith(("https://", "http://", "t.me/")):
        url = urlparse(text if "://" in text else "https://" + text)
        parts = [part for part in url.path.split("/") if part]
        if url.hostname not in ("t.me", "telegram.me") or not parts:
            raise ValueError("Use a t.me message link, username or numeric chat ID")
        if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
            return int("-100" + parts[1])
        if parts[0] in ("c", "joinchat") or parts[0].startswith("+"):
            raise ValueError("Invite links are not supported; join the chat first")
        text = parts[0]
    if re.fullmatch(r"-?[0-9]+", text):
        if int(text) == 0:
            raise ValueError("A monitored chat ID cannot be zero")
        return int(text)
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", text):
        return text.lstrip("@").lower()
    raise ValueError("Invalid monitored chat: {!r}".format(text))


@dataclass(frozen=True)
class MonitoredChat:
    """The inclusive first message and the last successfully processed message."""

    chat_id: ChatId
    start_message_id: int
    last_read_message_id: Optional[int] = None

    @property
    def cursor(self) -> int:
        if self.last_read_message_id is not None:
            return self.last_read_message_id
        return self.start_message_id - 1


@dataclass(frozen=True)
class MonitorConfig:
    """Listener settings from the video_monitor section of config.yaml."""

    chats: Tuple[MonitoredChat, ...] = ()
    interval: int = 60

    @property
    def enabled(self) -> bool:
        return bool(self.chats)

    @classmethod
    def from_config(cls, document: dict):
        section = document.get("video_monitor", {})
        if not isinstance(section, dict):
            raise ValueError("video_monitor must be a YAML mapping")
        enabled = section.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("video_monitor.enabled must be true or false")
        if not enabled:
            return cls()
        values = section.get("chats", [])
        if not isinstance(values, list):
            raise ValueError("video_monitor.chats must be a YAML list")
        interval = section.get("interval", 60)
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 10:
            raise ValueError("video_monitor.interval must be an integer of at least 10")
        chats = []
        seen = set()
        for entry in values:
            if not isinstance(entry, dict) or "chat_id" not in entry:
                raise ValueError("Each video_monitor chat needs chat_id")
            chat_id = parse_chat(entry["chat_id"])
            if chat_id in seen:
                raise ValueError("Duplicate video_monitor chat_id: {}".format(chat_id))
            seen.add(chat_id)
            start = entry.get("start_message_id")
            if isinstance(start, bool) or not isinstance(start, int) or start < 1:
                raise ValueError("start_message_id must be an integer of at least 1")
            last_read = entry.get("last_read_message_id")
            if "last_read_message_id" in entry and (
                isinstance(last_read, bool)
                or not isinstance(last_read, int)
                or last_read < 0
            ):
                raise ValueError("last_read_message_id must be a nonnegative integer")
            chats.append(MonitoredChat(chat_id, start, last_read))
        return cls(tuple(chats), interval)


class MonitorState:
    """Write progress into config.yaml without changing unrelated settings.

    The caller shares its round-trip YAML document with this object. Only after
    replacing the saved file successfully is the in-memory document advanced.
    Docker must bind-mount the containing directory to permit atomic replacement.
    """

    def __init__(self, filename: str, document: dict):
        self.path = Path(filename).resolve()
        self.document = document
        config = MonitorConfig.from_config(document)
        self.chats = {chat.chat_id: chat.cursor for chat in config.chats}

    def get(self, chat_id: ChatId) -> int:
        return self.chats[chat_id]

    def save(self, chat_id: ChatId, cursor: int):
        updated = deepcopy(self.document)
        index = next(
            index
            for index, entry in enumerate(updated["video_monitor"]["chats"])
            if parse_chat(entry["chat_id"]) == chat_id
        )
        updated["video_monitor"]["chats"][index]["last_read_message_id"] = cursor
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(self.path.parent), delete=False
            ) as stream:
                temporary = stream.name
                YAML().dump(updated, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, self.path.stat().st_mode & 0o777)
            try:
                os.replace(temporary, self.path)
            except OSError as error:
                if error.errno == errno.EBUSY:
                    raise OSError(
                        "Cannot save monitor progress to a file bind mount. "
                        "Mount the config directory and use --config "
                        "/app/settings/config.yaml; see compose.monitor.yaml"
                    ) from error
                raise
            self.document["video_monitor"]["chats"][index][
                "last_read_message_id"
            ] = cursor
            self.chats[chat_id] = cursor
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


def is_video(message) -> bool:
    """Include video attachments sent as documents, but exclude other files."""
    if getattr(message, "empty", False):
        return False
    if getattr(message, "video", None):
        return True
    document = getattr(message, "document", None)
    return bool(
        document and (getattr(document, "mime_type", None) or "").startswith("video/")
    )


class VideoMonitor:
    """Use the listener's history polling pattern and the existing download queue.

    ``history(chat_id, after_id, limit)`` yields messages in ascending order.
    ``download(chat_id, message)`` returns True only on success or an intentional
    skip (an existing complete file, or an excluded file format). No checkpoint
    passes a failed download, even if a later concurrent download succeeds.
    """

    def __init__(
        self, config: MonitorConfig, client, history, download, state: MonitorState
    ):
        self.config = config
        self.client = client
        self.history = history
        self.download = download
        self.state = state
        self.active_chats: Dict[int, ChatId] = {}
        self.initialized = set()

    def initialize(self, reference: ChatId):
        if reference in self.initialized:
            return
        cursor = self.state.get(reference)
        # Verify persistence before scheduling downloads, including on restart.
        self.state.save(reference, cursor)
        self.initialized.add(reference)
        logger.info(
            "Video monitor initialized chat=%s after_message=%s", reference, cursor
        )

    async def poll_chat(self, chat_id: int, reference: Optional[ChatId] = None) -> bool:
        """Process one bounded page; return whether another page may be ready."""
        reference = chat_id if reference is None else reference
        self.initialize(reference)
        cursor = self.state.get(reference)
        messages = {}
        async for message in self.history(chat_id, cursor, 100):
            if message.id > cursor:
                messages[message.id] = message
        ordered = [messages[key] for key in sorted(messages)]
        if not ordered:
            return False

        async def process(message):
            if not is_video(message):
                return True
            try:
                return await self.download(chat_id, message)
            except Exception as error:
                return error

        pending = [asyncio.create_task(process(message)) for message in ordered]
        blocked = False
        try:
            for message, task in zip(ordered, pending):
                result = await task
                if result is not True:
                    logger.warning(
                        "Video monitor will retry chat=%s message=%s: %s",
                        chat_id,
                        message.id,
                        result,
                    )
                    blocked = True
                if not blocked:
                    # Persist each contiguous completion, even while a later
                    # download is still running. Never advance over a failure.
                    self.state.save(reference, message.id)
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        return len(ordered) == 100 and not blocked

    async def _watch(self, reference: ChatId):
        chat_id = None
        while True:
            try:
                if chat_id is None:
                    chat = await self.client.get_chat(reference)
                    if chat.id in self.active_chats:
                        logger.warning(
                            "Ignoring duplicate monitored chat %s", reference
                        )
                        return
                    chat_id = chat.id
                    self.active_chats[chat_id] = reference
                    logger.info("Monitoring videos in chat=%s (%s)", chat_id, reference)
                while await self.poll_chat(chat_id, reference):
                    # Drain a restart backlog without waiting between pages.
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Video monitor failed for %s; will retry", reference)
            await asyncio.sleep(self.config.interval)

    async def run(self):
        await asyncio.gather(*(self._watch(chat.chat_id) for chat in self.config.chats))

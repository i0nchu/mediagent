"""Concise, sanitized operational logging for user-visible commands."""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, TextIO, TypeVar
from urllib.parse import urlsplit, urlunsplit

from mediagent.core.redaction import redact_text


DEFAULT_PROGRESS_INTERVAL_SECONDS = 60.0
MAX_LOG_MESSAGE_CHARS = 2_000
T = TypeVar("T")

_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_FILE_URL_PATTERN = re.compile(r"file://[^\r\n,;]+", re.IGNORECASE)
_WINDOWS_PATH_PATTERN = re.compile(r"(?i)(?<!\w)(?:[a-z]:\\|\\\\)[^\r\n,;]+")
_SPACED_POSIX_PATH_PATTERN = re.compile(r"(?<![\w:])/(?:[^/\s]+/)+[^\r\n,;]+")
_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![\w:])/(?:[^/\s]+/)*[^/\s,;]+")
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]+")
_EMOJI_PATTERN = re.compile(
    "["
    "\u00A9\u00AE"
    "\u203C\u2049\u20E3\u2122\u2139"
    "\u2190-\u21FF"
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "\u2300-\u23FF"
    "\u2600-\u27BF"
    "\u2B00-\u2BFF"
    "\u3030\u303D\u3297\u3299"
    "\uFE0E-\uFE0F"
    "\u200D"
    "]+"
)
_COMPONENT_PATTERN = re.compile(r"[^a-z0-9_.-]+")


def sanitize_log_text(value: object) -> str:
    """Return one bounded log line without credentials, signed queries, or Emoji."""

    text = str(value)
    sanitized_urls: list[str] = []

    def hold_url(match: re.Match[str]) -> str:
        sanitized_urls.append(_sanitize_url(match.group(0)))
        return f"MEDIAGENTURLPLACEHOLDER{len(sanitized_urls) - 1}"

    text = _URL_PATTERN.sub(hold_url, text)
    text = _FILE_URL_PATTERN.sub("<path>", text)
    text = redact_text(text)
    text = _WINDOWS_PATH_PATTERN.sub("<path>", text)
    text = _SPACED_POSIX_PATH_PATTERN.sub("<path>", text)
    text = _ABSOLUTE_PATH_PATTERN.sub(_sanitize_path_match, text)
    for index, sanitized_url in enumerate(sanitized_urls):
        text = text.replace(f"MEDIAGENTURLPLACEHOLDER{index}", sanitized_url)
    text = _EMOJI_PATTERN.sub("", text)
    text = "".join(character for character in text if unicodedata.category(character) != "Cf")
    text = _CONTROL_PATTERN.sub(" ", text)
    text = " ".join(text.split())
    if len(text) > MAX_LOG_MESSAGE_CHARS:
        text = f"{text[: MAX_LOG_MESSAGE_CHARS - 3].rstrip()}..."
    return text


def _sanitize_path_match(match: re.Match[str]) -> str:
    raw = match.group(0).rstrip(".")
    name = raw.rsplit("/", 1)[-1]
    suffix = match.group(0)[len(raw) :]
    return f"<path>/{name or 'root'}{suffix}"


def _sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if not hostname:
            return "<redacted-url>"
        netloc = hostname
        if ":" in hostname and not hostname.startswith("["):
            netloc = f"[{hostname}]"
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))
    except (TypeError, ValueError):
        return "<redacted-url>"


def _component_name(value: str) -> str:
    normalized = _COMPONENT_PATTERN.sub("-", value.strip().lower()).strip("-.")
    return normalized[:48] or "mediagent"


def _level_from_env(env: Mapping[str, str]) -> int:
    configured = env.get("MEDIAGENT_LOG_LEVEL", "INFO").strip().upper()
    if configured == "WARN":
        configured = "WARNING"
    return {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }.get(configured, logging.INFO)


class OperationalLogFormatter(logging.Formatter):
    """Render stable UTC log lines without relying on process locale."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        level = "WARN" if record.levelno == logging.WARNING else record.levelname
        component = _component_name(str(getattr(record, "operation", "mediagent")))
        message = sanitize_log_text(record.getMessage())
        return f"{timestamp} {level:<5} {component} {message}"


class OperationReporter(Protocol):
    """Logging surface available to tools without configuring output sinks."""

    def debug(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...

    def error(self, message: str) -> None: ...


class NullOperationLogger:
    """Silent default for library use and offline tests."""

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


@dataclass
class OperationLogger:
    """Small facade that keeps command logs consistent and low-noise."""

    operation: str
    logger: logging.Logger
    clock: Callable[[], float] = time.monotonic
    _last_emitted_at: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._last_emitted_at = self.clock()

    @classmethod
    def create(
        cls,
        operation: str,
        *,
        env: Mapping[str, str],
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> "OperationLogger":
        component = _component_name(operation)
        logger = logging.Logger(f"mediagent.operation.{component}", level=_level_from_env(env))
        logger.propagate = False
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(OperationalLogFormatter())
        logger.addHandler(handler)
        return cls(operation=component, logger=logger, clock=clock)

    def debug(self, message: str) -> None:
        self._write(logging.DEBUG, message)

    def info(self, message: str) -> None:
        self._write(logging.INFO, message)

    def warning(self, message: str) -> None:
        self._write(logging.WARNING, message)

    def error(self, message: str) -> None:
        self._write(logging.ERROR, message)

    def started(self, *, dry_run: bool) -> None:
        self.info("Started a dry run." if dry_run else "Started.")

    def completed(self, *, elapsed_seconds: float, warning_count: int = 0) -> None:
        message = f"Completed successfully in {elapsed_seconds:.1f} seconds."
        if warning_count:
            message = f"{message} The result contains {warning_count} warning{'s' if warning_count != 1 else ''}."
        self.info(message)

    def failed(self, *, elapsed_seconds: float, reason: str, next_action: str) -> None:
        self.error(
            f"Failed after {elapsed_seconds:.1f} seconds: {reason} "
            f"Next action: {next_action}"
        )

    def _write(self, level: int, message: str) -> None:
        self.logger.log(level, message, extra={"operation": self.operation})
        if self.logger.isEnabledFor(level):
            self._last_emitted_at = self.clock()

    def seconds_since_output(self) -> float:
        return max(0.0, self.clock() - self._last_emitted_at)


async def await_with_heartbeat(
    awaitable: Awaitable[T],
    operation_log: OperationLogger,
    *,
    interval_seconds: float = DEFAULT_PROGRESS_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Await one operation while a background heartbeat survives blocking work."""

    started_at = clock()
    stop = threading.Event()

    def heartbeat() -> None:
        last_heartbeat_at = started_at
        while True:
            quiet_for = operation_log.seconds_since_output()
            heartbeat_quiet_for = max(0.0, clock() - last_heartbeat_at)
            wait_seconds = max(
                0.01,
                interval_seconds - min(quiet_for, heartbeat_quiet_for),
            )
            if stop.wait(wait_seconds):
                return
            if (
                operation_log.seconds_since_output() >= interval_seconds
                and clock() - last_heartbeat_at >= interval_seconds
            ):
                last_heartbeat_at = clock()
                operation_log.info(
                    f"Still working; {_format_elapsed(clock() - started_at)} elapsed."
                )

    worker = threading.Thread(
        target=heartbeat,
        name=f"mediagent-heartbeat-{operation_log.operation}",
        daemon=True,
    )
    worker.start()
    try:
        return await awaitable
    finally:
        stop.set()
        worker.join(timeout=max(0.1, min(interval_seconds, 1.0)))


@dataclass
class ProgressLogger:
    """Emit aggregate progress at a bounded time interval."""

    operation_log: OperationReporter
    interval_seconds: float = DEFAULT_PROGRESS_INTERVAL_SECONDS
    clock: Callable[[], float] = time.monotonic
    _started_at: float = field(default=0.0, init=False)
    _last_emitted_at: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._started_at = self.clock()
        self._last_emitted_at = self._started_at

    def report(
        self,
        *,
        completed: int,
        pending: int | None = None,
        failed: int = 0,
        force: bool = False,
    ) -> bool:
        now = self.clock()
        if (
            not force
            and self._last_emitted_at is not None
            and now - self._last_emitted_at < self.interval_seconds
        ):
            return False
        parts = [f"{completed} completed"]
        if pending is not None:
            parts.append(f"{pending} pending")
        parts.append(f"{failed} failed")
        parts.append(f"{_format_elapsed(now - self._started_at)} elapsed")
        self.operation_log.info(f"Progress: {', '.join(parts)}.")
        self._last_emitted_at = now
        return True


def _format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"

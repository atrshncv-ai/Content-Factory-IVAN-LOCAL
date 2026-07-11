"""Субагент «SMM-Паблишер».

Две зоны ответственности:
  1. publish() — отправка медиа + текста в SaaS автопостинга через внешнюю
     CLI-утилиту (syscall), как требует AGENTS.md: падежная логика изолирована.
  2. collect_metrics() — сбор многоуровневых метрик (1d/1w/1m) с записью
     в таблицу metrics (core/db.py).

Весь I/O — за единым швом _run_command (граница мокирования). Любой сбой CLI
(ошибка, таймаут, битый JSON) оборачивается в PublishError. Ретраев внутри
нет: повтор — на следующем cron-тике Оркестратора (событийная модель).
"""
import json
import subprocess
from pathlib import Path
from typing import Any

from core.config import Settings
from core.db import Database

PERIODS = ("1d", "1w", "1m")


class PublishError(RuntimeError):
    """Сбой внешней CLI-утилиты автопостинга: ошибка, таймаут или битый ответ."""


def _run_command(command: list[str]) -> str:
    """Запуск внешней CLI-утилиты как syscall. Returns: stdout.

    Raises:
        subprocess.CalledProcessError: утилита завершилась с ошибкой.
        subprocess.TimeoutExpired: утилита не ответила вовремя.
    """
    result = subprocess.run(
        command, capture_output=True, text=True, check=True, timeout=120,
    )
    return result.stdout


class Publisher:
    """SMM-Паблишер: постинг и сбор метрик через CLI автопостинга.

    Example:
        >>> publisher = Publisher(Settings(), db)
        >>> post_id = publisher.publish("/shared/final.png", "Новый пост #ai")
        >>> publisher.collect_metrics(post_id, "1d")
    """

    def __init__(self, settings: Settings, db: Database) -> None:
        self._settings = settings
        self._db = db

    def publish(self, media_path: str, caption: str) -> str:
        """Публикует медиа с подписью. Returns: post_id опубликованного поста.

        Raises:
            PublishError: файл не найден, CLI упала/зависла или вернула мусор.
        """
        if not Path(media_path).is_file():
            raise PublishError(f"файл не найден: {media_path}")
        try:
            stdout = _run_command([
                self._settings.autopost_command, "publish", media_path,
                "--caption", caption, "--json",
            ])
            post_id = str(json.loads(stdout).get("post_id", ""))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise PublishError(f"CLI автопостинга недоступна: {exc}") from exc
        except (json.JSONDecodeError, AttributeError) as exc:
            raise PublishError(f"битый ответ CLI автопостинга: {exc}") from exc
        if not post_id:
            raise PublishError("CLI автопостинга не вернула post_id")
        self._db.log("INFO", "publisher", f"опубликован пост {post_id} ({media_path})")
        return post_id

    def collect_metrics(self, post_id: str, period: str) -> dict[str, Any]:
        """Собирает метрики поста за период и пишет в таблицу metrics.

        Args:
            period: '1d' | '1w' | '1m' (проверяется до вызова CLI;
                    CHECK-ограничение в БД — вторая линия защиты).

        Returns:
            {post_id, period, likes, comments, reach}.

        Raises:
            ValueError: период вне 1d/1w/1m.
            PublishError: CLI упала/зависла или вернула мусор.
        """
        if period not in PERIODS:
            raise ValueError(f"недопустимый период: {period!r} (нужно 1d/1w/1m)")
        try:
            stdout = _run_command([
                self._settings.autopost_command, "stats", post_id,
                "--period", period, "--json",
            ])
            data = json.loads(stdout)
            likes, comments, reach = (
                int(data["likes"]), int(data["comments"]), int(data["reach"]),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise PublishError(f"CLI метрик недоступна: {exc}") from exc
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError) as exc:
            raise PublishError(f"битый ответ CLI метрик: {exc}") from exc
        self._db.add_metric(post_id, period, likes, comments, reach)
        self._db.log(
            "INFO", "publisher",
            f"метрики {post_id} ({period}): likes={likes} comments={comments} reach={reach}",
        )
        return {
            "post_id": post_id, "period": period,
            "likes": likes, "comments": comments, "reach": reach,
        }

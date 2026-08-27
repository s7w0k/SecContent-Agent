"""Publish Lock - 跨进程发布互斥（Phase2 / Phase7 并发一致性）。

单实例用文件锁；多实例应替换为 Redis distributed lock（同接口）。
锁语义：
  - owner token：只有持锁者能解锁
  - TTL：超时后其他实例可接管（处理崩溃遗留）
  - 心跳续约：长发布可持续重置 TTL
  - crash recovery：检测到过期锁时按 owner 信息决定是否接管
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from pathlib import Path

logger = logging.getLogger("backend.agent.wiki.locks")

LOCK_FILENAME = ".publish.lock"


class PublishLock:
    """基于文件 + TTL 的发布锁（owner token 保障 owner-only unlock）。"""

    def __init__(
        self,
        lock_path: str | Path,
        *,
        ttl_seconds: float = 120.0,
        renewal_interval: float = 30.0,
    ):
        self.lock_path = Path(lock_path)
        self.ttl = ttl_seconds
        self.renewal_interval = renewal_interval
        self._token: str | None = None

    # ── 低层文件读写 ──────────────────────────────────────

    def _read(self) -> dict:
        if not self.lock_path.exists():
            return {}
        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_owned(self, token: str) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "owner": token,
            "acquired_at": time.time(),
            "expires_at": time.time() + self.ttl,
            "pid": _own_pid(),
        }
        tmp = self.lock_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.lock_path)

    def _is_expired(self, data: dict) -> bool:
        exp = data.get("expires_at")
        return exp is None or float(exp) <= time.time()

    # ── 公开接口 ──────────────────────────────────────────

    def acquire(self) -> bool:
        data = self._read()
        if data:
            if not self._is_expired(data):
                return False  # 活跃锁，被占用
            logger.warning("接管过期锁 owner=%s", data.get("owner"))
        self._token = uuid.uuid4().hex
        self._write_owned(self._token)
        return True

    def owns(self, token: str | None = None) -> bool:
        token = token or self._token
        if not token:
            return False
        data = self._read()
        return data.get("owner") == token

    def renew(self) -> bool:
        if not self.owns():
            return False
        self._write_owned(self._token)  # type: ignore[arg-type]
        return True

    def release(self) -> bool:
        if not self.owns():
            return False  # owner-only unlock
        self._token = None
        with contextlib.suppress(FileNotFoundError):
            self.lock_path.unlink()
        return True

    # 供发布循环使用：持续续约的简单封装（在异步任务中由调用方驱动）

    def heartbeat(self, stop_flag_holder: list[bool] | None = None) -> None:
        """循环续约，直到 stop_flag_holder[0] 为 True。仅供存在事件循环的发布流程使用。"""
        while not (stop_flag_holder and stop_flag_holder[0]):
            if not self.renew():
                logger.warning("续约失败，可能是锁已丢失")
                return
            time.sleep(self.renewal_interval)


def _own_pid() -> int | None:
    try:
        import os

        return os.getpid()
    except Exception:
        return None

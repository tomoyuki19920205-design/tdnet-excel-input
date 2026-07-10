import time
import random
import logging
import functools
from typing import Callable, Any

logger = logging.getLogger(__name__)

def with_retry(
    max_tries: int = 3,
    status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504),
    backoff_factor: float = 1.0,
    max_delay: float = 30.0,
) -> Callable:
    """
    リクエストの一時的な失敗（429や500系エラーなど）に対して、
    Retry-Afterヘッダーを尊重しつつ、Exponential Backoff (Jitter付き) でリトライを行うデコレータ。
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            tries = 0
            while tries < max_tries:
                tries += 1
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    # Requests, HTTPX, postgrest 等からステータスコードを抽出
                    status_code = getattr(getattr(e, 'response', None), 'status_code', None)
                    
                    if status_code is None:
                        # postgrest.exceptions.APIError 対策
                        details = getattr(e, 'details', {})
                        if isinstance(details, dict):
                            # detailsの中身から探す等のヒューリスティック
                            status_code = details.get('code')
                            try:
                                status_code = int(status_code) if status_code else None
                            except ValueError:
                                status_code = None
                    
                    if status_code and status_code not in status_forcelist:
                        # リトライ対象外のステータスコードなら即座に再送せずraise
                        raise

                    if tries >= max_tries:
                        logger.error(f"[RETRY] Max tries ({max_tries}) reached for {func.__name__}. Error: {e}")
                        raise

                    # Retry-Afterヘッダーがあれば最優先する
                    retry_after = None
                    headers = getattr(getattr(e, 'response', None), 'headers', {})
                    if headers and "Retry-After" in headers:
                        try:
                            retry_after = float(headers["Retry-After"])
                        except (ValueError, TypeError):
                            pass
                    
                    if retry_after is not None:
                        delay = retry_after
                    else:
                        # 指数バックオフ + Jitter
                        delay = backoff_factor * (2 ** (tries - 1))
                        delay = min(delay, max_delay)
                        # Full Jitter (0からdelayの間のランダム値)
                        delay = random.uniform(0, delay)

                    logger.warning(
                        f"[RETRY] {func.__name__} failed on try {tries}/{max_tries} "
                        f"(status={status_code}). Retrying in {delay:.2f}s... Error: {e}"
                    )
                    time.sleep(delay)
            return func(*args, **kwargs) # Fallback (should not be reached)
        return wrapper
    return decorator

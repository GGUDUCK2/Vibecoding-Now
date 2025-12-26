"""
재시도 유틸리티 모듈

지수 백오프 + 지터를 적용한 재시도 정책 구현
- 최대 3회 재시도
- 지수 백오프 (1s → 2s → 4s)
- 10~20% 랜덤 지터
- 400대 에러는 재시도 안함 (비즈니스 로직 오류)
"""

import functools
import logging
import random
import time
from typing import Callable, Optional, Tuple, Type, TypeVar

import requests

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryableError(Exception):
    """재시도 가능한 오류를 나타내는 예외"""
    pass


class NonRetryableError(Exception):
    """재시도하면 안 되는 오류를 나타내는 예외"""
    pass


def is_retryable_error(exception: Exception) -> bool:
    """
    재시도 가능한 오류인지 판단한다.
    
    재시도 가능:
    - 5xx 서버 오류
    - 네트워크 타임아웃
    - 연결 오류
    
    재시도 불가:
    - 4xx 클라이언트 오류 (잘못된 요청)
    - NonRetryableError
    """
    if isinstance(exception, NonRetryableError):
        return False
    
    if isinstance(exception, RetryableError):
        return True
    
    if isinstance(exception, requests.exceptions.HTTPError):
        response = exception.response
        if response is not None:
            # 4xx 오류는 재시도 안함
            if 400 <= response.status_code < 500:
                return False
            # 5xx 오류는 재시도
            if response.status_code >= 500:
                return True
        return False
    
    # 네트워크 관련 오류는 재시도
    if isinstance(exception, (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
        ConnectionError,
        TimeoutError,
    )):
        return True
    
    return False


def calculate_delay(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    jitter_range: Tuple[float, float] = (0.1, 0.2)
) -> float:
    """
    지수 백오프 + 지터를 적용한 대기 시간을 계산한다.
    
    Args:
        attempt: 현재 재시도 횟수 (0부터 시작)
        base_delay: 기본 대기 시간 (초)
        max_delay: 최대 대기 시간 (초)
        jitter_range: 지터 범위 (min, max) - 비율로 표현
    
    Returns:
        계산된 대기 시간 (초)
    """
    # 지수 백오프: 1s, 2s, 4s, ...
    delay = base_delay * (2 ** attempt)
    delay = min(delay, max_delay)
    
    # 지터 추가 (10~20% 랜덤 변동)
    jitter_min, jitter_max = jitter_range
    jitter = delay * random.uniform(jitter_min, jitter_max)
    delay += jitter
    
    return delay


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    jitter_range: Tuple[float, float] = (0.1, 0.2),
    retryable_exceptions: Optional[Tuple[Type[Exception], ...]] = None
) -> Callable:
    """
    재시도 데코레이터
    
    Args:
        max_attempts: 최대 시도 횟수 (재시도 포함)
        base_delay: 기본 대기 시간
        max_delay: 최대 대기 시간
        jitter_range: 지터 범위
        retryable_exceptions: 재시도할 예외 타입들 (없으면 is_retryable_error 사용)
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception: Optional[Exception] = None
            
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    
                    # 재시도 가능 여부 확인
                    should_retry = False
                    if retryable_exceptions:
                        should_retry = isinstance(e, retryable_exceptions)
                    else:
                        should_retry = is_retryable_error(e)
                    
                    if not should_retry:
                        logger.warning(
                            f"[{func.__name__}] 재시도 불가능한 오류 발생: {type(e).__name__}: {e}"
                        )
                        raise
                    
                    # 마지막 시도였으면 예외 발생
                    if attempt == max_attempts - 1:
                        logger.error(
                            f"[{func.__name__}] 최대 재시도 횟수({max_attempts}) 초과: {e}"
                        )
                        raise
                    
                    # 대기 시간 계산 및 대기
                    delay = calculate_delay(attempt, base_delay, max_delay, jitter_range)
                    logger.info(
                        f"[{func.__name__}] 재시도 {attempt + 1}/{max_attempts - 1}, "
                        f"{delay:.2f}초 후 재시도: {type(e).__name__}"
                    )
                    time.sleep(delay)
            
            # 이 코드에 도달하면 안 됨
            if last_exception:
                raise last_exception
            raise RuntimeError("예상치 못한 재시도 루프 종료")
        
        return wrapper
    return decorator


class RetryContext:
    """
    컨텍스트 매니저 스타일의 재시도 유틸리티
    
    사용 예:
        retry_ctx = RetryContext(max_attempts=3)
        for attempt in retry_ctx:
            try:
                result = some_operation()
                break
            except Exception as e:
                if not retry_ctx.should_retry(e):
                    raise
    """
    
    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 10.0,
        jitter_range: Tuple[float, float] = (0.1, 0.2)
    ):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter_range = jitter_range
        self._current_attempt = 0
        self._last_exception: Optional[Exception] = None
    
    def __iter__(self):
        self._current_attempt = 0
        return self
    
    def __next__(self) -> int:
        if self._current_attempt >= self.max_attempts:
            raise StopIteration
        
        attempt = self._current_attempt
        self._current_attempt += 1
        return attempt
    
    def should_retry(self, exception: Exception) -> bool:
        """재시도 여부를 판단하고 필요시 대기한다."""
        self._last_exception = exception
        
        if not is_retryable_error(exception):
            return False
        
        if self._current_attempt >= self.max_attempts:
            return False
        
        delay = calculate_delay(
            self._current_attempt - 1,
            self.base_delay,
            self.max_delay,
            self.jitter_range
        )
        logger.info(
            f"재시도 {self._current_attempt}/{self.max_attempts}, "
            f"{delay:.2f}초 후 재시도: {type(exception).__name__}"
        )
        time.sleep(delay)
        return True

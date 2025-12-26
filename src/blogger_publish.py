"""
Blogger API 발행 모듈

Blogger API v3를 사용하여 예약 발행을 수행한다.
- Service Account 인증
- RFC3339 형식의 예약 시간 설정
- 라벨링 지원
"""

import json
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from content_generator import GeneratedContent
from retry import NonRetryableError, RetryableError, with_retry

logger = logging.getLogger(__name__)

# Blogger API 스코프
BLOGGER_SCOPES = ["https://www.googleapis.com/auth/blogger"]


def parse_time_range(time_range: str) -> tuple[int, int, int, int]:
    """
    시간 범위 문자열을 파싱한다.
    
    Args:
        time_range: "HH:MM-HH:MM" 형식
    
    Returns:
        (start_hour, start_minute, end_hour, end_minute) 튜플
    """
    start, end = time_range.split("-")
    start_h, start_m = map(int, start.split(":"))
    end_h, end_m = map(int, end.split(":"))
    return start_h, start_m, end_h, end_m


def calculate_random_publish_time(
    time_range: str,
    timezone: str = "Asia/Seoul"
) -> datetime:
    """
    지정된 시간 범위 내에서 랜덤한 발행 시간을 계산한다.
    
    Args:
        time_range: "HH:MM-HH:MM" 형식
        timezone: 타임존 (기본: Asia/Seoul)
    
    Returns:
        랜덤 발행 시간 (datetime)
    """
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    
    start_h, start_m, end_h, end_m = parse_time_range(time_range)
    
    # 오늘 날짜 기준으로 시작/종료 시간 계산
    start_time = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end_time = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    
    # 현재 시간이 발행 시간대를 지났으면 다음 날로 설정
    if now > end_time:
        start_time += timedelta(days=1)
        end_time += timedelta(days=1)
    
    # 시간 범위 내에서 랜덤 초 선택
    total_seconds = int((end_time - start_time).total_seconds())
    random_seconds = random.randint(0, max(0, total_seconds))
    
    publish_time = start_time + timedelta(seconds=random_seconds)
    
    logger.info(f"발행 예약 시간: {publish_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    return publish_time


def datetime_to_rfc3339(dt: datetime) -> str:
    """datetime을 RFC3339 형식 문자열로 변환한다."""
    return dt.isoformat()


class BloggerPublisher:
    """Blogger API 발행자"""
    
    def __init__(
        self,
        blog_id: str,
        credentials_json: Optional[str] = None,
        credentials_path: Optional[Path] = None,
    ):
        """
        Args:
            blog_id: Blogger 블로그 ID
            credentials_json: Service Account 인증 JSON 문자열
            credentials_path: Service Account 인증 JSON 파일 경로
        """
        self.blog_id = blog_id
        self._service = None
        
        # 인증 설정
        if credentials_json:
            creds_data = json.loads(credentials_json)
            credentials = service_account.Credentials.from_service_account_info(
                creds_data, scopes=BLOGGER_SCOPES
            )
        elif credentials_path:
            credentials = service_account.Credentials.from_service_account_file(
                str(credentials_path), scopes=BLOGGER_SCOPES
            )
        else:
            raise ValueError("credentials_json 또는 credentials_path 필요")
        
        self._service = build("blogger", "v3", credentials=credentials)
        logger.info(f"Blogger API 초기화 완료 (Blog ID: {blog_id})")
    
    @with_retry(max_attempts=3, base_delay=2.0)
    def publish_scheduled(
        self,
        content: GeneratedContent,
        publish_time: datetime,
        dry_run: bool = False
    ) -> Optional[dict]:
        """
        콘텐츠를 예약 발행한다.
        
        Args:
            content: 발행할 콘텐츠
            publish_time: 발행 예약 시간
            dry_run: True면 실제 발행하지 않음
        
        Returns:
            발행된 포스트 정보 또는 None
        """
        logger.info(f"발행 시작: {content.title[:30]}...")
        
        post_body = {
            "kind": "blogger#post",
            "blog": {"id": self.blog_id},
            "title": content.title,
            "content": content.content,
            "labels": content.labels,
            "published": datetime_to_rfc3339(publish_time),
        }
        
        if dry_run:
            logger.info(f"[DRY-RUN] 발행 스킵: {content.title}")
            logger.info(f"[DRY-RUN] 라벨: {content.labels}")
            logger.info(f"[DRY-RUN] 예약 시간: {publish_time}")
            return {
                "id": "dry-run-id",
                "url": "https://example.com/dry-run",
                "title": content.title,
                "published": datetime_to_rfc3339(publish_time),
            }
        
        try:
            result = self._service.posts().insert(
                blogId=self.blog_id,
                body=post_body,
                isDraft=False,
            ).execute()
            
            logger.info(f"발행 성공: {result.get('url')}")
            return result
        
        except HttpError as e:
            status_code = e.resp.status if e.resp else 0
            
            # 4xx 오류는 재시도 안함
            if 400 <= status_code < 500:
                logger.error(f"Blogger API 클라이언트 오류 ({status_code}): {e}")
                raise NonRetryableError(f"클라이언트 오류: {e}") from e
            
            # 5xx 오류는 재시도
            logger.error(f"Blogger API 서버 오류 ({status_code}): {e}")
            raise RetryableError(f"서버 오류: {e}") from e
        
        except Exception as e:
            logger.error(f"발행 실패: {e}")
            raise RetryableError(f"발행 실패: {e}") from e
    
    def get_post(self, post_id: str) -> Optional[dict]:
        """포스트 정보를 조회한다."""
        try:
            return self._service.posts().get(
                blogId=self.blog_id, postId=post_id
            ).execute()
        except HttpError as e:
            logger.error(f"포스트 조회 실패: {e}")
            return None

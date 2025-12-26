"""
Telegram 알림 모듈

발행 결과를 Telegram으로 알린다.
- 성공/실패 알림
- 에러 상세 정보
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests

from retry import with_retry

logger = logging.getLogger(__name__)


@dataclass
class NotificationResult:
    """알림 전송 결과"""
    success: bool
    message_id: Optional[int] = None
    error: Optional[str] = None


class TelegramNotifier:
    """Telegram 알림 발송자"""
    
    TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._api_url = self.TELEGRAM_API_URL.format(token=bot_token)
    
    @with_retry(max_attempts=3, base_delay=1.0)
    def _send_message(self, text: str, parse_mode: str = "HTML") -> NotificationResult:
        """
        메시지를 전송한다.
        
        Args:
            text: 전송할 메시지
            parse_mode: 파싱 모드 (HTML/Markdown)
        
        Returns:
            NotificationResult
        """
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        
        try:
            response = requests.post(self._api_url, json=payload, timeout=30)
            response.raise_for_status()
            
            result = response.json()
            if result.get("ok"):
                message_id = result.get("result", {}).get("message_id")
                logger.info(f"Telegram 알림 전송 성공: message_id={message_id}")
                return NotificationResult(success=True, message_id=message_id)
            else:
                error = result.get("description", "Unknown error")
                logger.warning(f"Telegram API 오류: {error}")
                return NotificationResult(success=False, error=error)
        
        except requests.exceptions.RequestException as e:
            logger.error(f"Telegram 전송 실패: {e}")
            raise
    
    def notify_publish_success(
        self,
        category: str,
        title: str,
        scheduled_time: datetime,
        post_url: Optional[str] = None,
    ) -> NotificationResult:
        """
        발행 예약 성공 알림을 전송한다.
        """
        time_str = scheduled_time.strftime("%H:%M")
        
        message = f"""[VibeCoding Now] 포스팅 예약 완료 🚀

<b>카테고리:</b> {category}
<b>제목:</b> {title}
<b>발행 예정:</b> {time_str}"""
        
        if post_url:
            message += f"\n<b>URL:</b> {post_url}"
        
        try:
            return self._send_message(message)
        except Exception as e:
            logger.error(f"성공 알림 전송 실패: {e}")
            return NotificationResult(success=False, error=str(e))
    
    def notify_no_new_content(self) -> NotificationResult:
        """
        신규 발행 없음 알림을 전송한다.
        """
        message = """[VibeCoding Now] 실행 완료

신규 발행 없음"""
        
        try:
            return self._send_message(message)
        except Exception as e:
            logger.error(f"없음 알림 전송 실패: {e}")
            return NotificationResult(success=False, error=str(e))
    
    def notify_error(
        self,
        error_type: str,
        error_message: str,
        details: Optional[str] = None,
    ) -> NotificationResult:
        """
        오류 알림을 전송한다.
        """
        message = f"""[VibeCoding Now] ⚠️ 오류 발생

<b>타입:</b> {error_type}
<b>원인:</b> {error_message}
<b>조치 필요</b>"""
        
        if details:
            message += f"\n\n<b>상세:</b>\n<code>{details[:500]}</code>"
        
        try:
            return self._send_message(message)
        except Exception as e:
            # 오류 알림 전송 실패는 로그만 남김
            logger.error(f"오류 알림 전송 실패: {e}")
            return NotificationResult(success=False, error=str(e))
    
    def notify_dry_run(
        self,
        category: str,
        title: str,
        scheduled_time: datetime,
    ) -> NotificationResult:
        """
        Dry-run 모드 알림을 전송한다.
        """
        time_str = scheduled_time.strftime("%H:%M")
        
        message = f"""[VibeCoding Now] 🧪 DRY-RUN 모드

<b>카테고리:</b> {category}
<b>제목:</b> {title}
<b>예정 시간:</b> {time_str}

(실제 발행되지 않음)"""
        
        try:
            return self._send_message(message)
        except Exception as e:
            logger.error(f"dry-run 알림 전송 실패: {e}")
            return NotificationResult(success=False, error=str(e))

"""
메인 오케스트레이터

전체 자동화 흐름을 조율한다:
1. RSS 피드 수집
2. 캐시 확인 (중복 스킵)
3. Gemini 콘텐츠 생성
4. Blogger 예약 발행
5. 캐시 업데이트
6. Telegram 알림
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml

# 모듈 경로 추가
sys.path.insert(0, str(Path(__file__).parent))

from blogger_publish import BloggerPublisher, calculate_random_publish_time
from content_generator import ContentGenerator
from notifier import TelegramNotifier
from rss_fetcher import RSSFetcher

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def get_category_config(config: dict, category: str) -> dict:
    """카테고리에 해당하는 스케줄 설정을 반환한다."""
    schedule = config.get("schedule", {})
    
    # category 매핑
    category_map = {
        "column": "column",
        "tips-concept": "tips_concept",
        "tips-practical": "tips_practical",
    }
    
    config_key = category_map.get(category, category)
    return schedule.get(config_key, {})


def get_rss_category(category: str) -> str:
    """CLI 카테고리를 RSS 카테고리로 변환한다."""
    if category == "column":
        return "column"
    return "tips"


def main():
    parser = argparse.ArgumentParser(description="VibeCoding Now 블로그 자동화")
    parser.add_argument(
        "--category",
        required=True,
        choices=["column", "tips-concept", "tips-practical"],
        help="발행 카테고리",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 발행 없이 테스트",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="설정 파일 경로",
    )
    
    args = parser.parse_args()
    
    # 경로 설정
    project_root = Path(__file__).parent.parent
    config_path = project_root / args.config
    cache_path = project_root / "cache" / "processed.json"
    
    logger.info(f"=== VibeCoding Now 자동화 시작 ===")
    logger.info(f"카테고리: {args.category}")
    logger.info(f"Dry-run: {args.dry_run}")
    
    # 설정 로드
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    
    # 환경 변수에서 시크릿 로드
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    blogger_credentials = os.environ.get("BLOGGER_CREDENTIALS")
    blog_id = os.environ.get("BLOG_ID")
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    # 필수 환경 변수 확인
    missing_vars = []
    if not gemini_api_key:
        missing_vars.append("GEMINI_API_KEY")
    if not blogger_credentials:
        missing_vars.append("BLOGGER_CREDENTIALS")
    if not blog_id:
        missing_vars.append("BLOG_ID")
    if not telegram_token:
        missing_vars.append("TELEGRAM_BOT_TOKEN")
    if not telegram_chat_id:
        missing_vars.append("TELEGRAM_CHAT_ID")
    
    if missing_vars and not args.dry_run:
        logger.error(f"필수 환경 변수 누락: {', '.join(missing_vars)}")
        sys.exit(1)
    
    # 알림 클라이언트 초기화 (가능한 경우)
    notifier = None
    if telegram_token and telegram_chat_id:
        notifier = TelegramNotifier(telegram_token, telegram_chat_id)
    
    try:
        # 1. RSS 피드 수집
        logger.info("1. RSS 피드 수집...")
        rss_fetcher = RSSFetcher(config_path, cache_path)
        
        rss_category = get_rss_category(args.category)
        max_posts = config.get("system", {}).get("max_posts_per_run", 1)
        
        new_items = rss_fetcher.fetch_new_items(rss_category, max_items=max_posts)
        
        if not new_items:
            logger.info("신규 항목 없음, 종료")
            if notifier:
                notifier.notify_no_new_content()
            return
        
        # 2. 콘텐츠 생성
        logger.info("2. 콘텐츠 생성...")
        
        if args.dry_run and not gemini_api_key:
            # Dry-run에서 API 키가 없으면 더미 콘텐츠 생성
            from content_generator import GeneratedContent
            from rss_fetcher import RSSItem
            
            item = new_items[0]
            content = GeneratedContent(
                title=f"[DRY-RUN] {item.title}",
                content="<p>Dry-run 테스트 콘텐츠입니다.</p>",
                labels=["컬럼"] if args.category == "column" else ["Tips"],
                source_item=item,
            )
        else:
            generator = ContentGenerator(config_path, gemini_api_key)
            item = new_items[0]
            content = generator.generate(item, args.category)
            
            if not content:
                logger.error("콘텐츠 생성 실패")
                if notifier:
                    notifier.notify_error(
                        "Content Generation Failure",
                        "Gemini API에서 콘텐츠를 생성하지 못했습니다.",
                    )
                sys.exit(1)
        
        # 3. 발행 시간 계산
        category_config = get_category_config(config, args.category)
        time_range = category_config.get("time_range", "07:00-07:10")
        timezone = config.get("system", {}).get("timezone", "Asia/Seoul")
        
        publish_time = calculate_random_publish_time(time_range, timezone)
        
        # 4. Blogger 발행
        logger.info("3. Blogger 예약 발행...")
        
        if args.dry_run:
            logger.info(f"[DRY-RUN] 발행 스킵: {content.title}")
            post_result = {
                "id": "dry-run-id",
                "url": "https://example.com/dry-run",
            }
        else:
            publisher = BloggerPublisher(
                blog_id=blog_id,
                credentials_json=blogger_credentials,
            )
            post_result = publisher.publish_scheduled(
                content, publish_time, dry_run=args.dry_run
            )
        
        if not post_result:
            logger.error("발행 실패")
            if notifier:
                notifier.notify_error(
                    "Blogger Publish Failure",
                    "Blogger API에서 포스트를 발행하지 못했습니다.",
                )
            sys.exit(1)
        
        # 5. 캐시 업데이트
        logger.info("4. 캐시 업데이트...")
        rss_fetcher.mark_as_processed(content.source_item)
        rss_fetcher.save_cache()
        
        # 6. 알림 전송
        logger.info("5. 알림 전송...")
        if notifier:
            label = category_config.get("label", args.category)
            if args.dry_run:
                notifier.notify_dry_run(label, content.title, publish_time)
            else:
                notifier.notify_publish_success(
                    category=label,
                    title=content.title,
                    scheduled_time=publish_time,
                    post_url=post_result.get("url"),
                )
        
        logger.info("=== 자동화 완료 ===")
    
    except Exception as e:
        logger.exception(f"자동화 실패: {e}")
        if notifier:
            notifier.notify_error(
                type(e).__name__,
                str(e),
            )
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
RSS 피드 수집 모듈

RSS 피드를 수집하고 GUID 기반 중복 체크를 수행한다.
- feedparser로 RSS 파싱
- GUID 기반 중복 확인
- fallback: title + published 해시
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import feedparser
import yaml

from retry import RetryableError, with_retry

logger = logging.getLogger(__name__)


@dataclass
class RSSItem:
    """RSS 피드 항목"""
    guid: str
    title: str
    link: str
    summary: str
    published: Optional[str]
    category: str
    source_name: str


class CacheManager:
    """처리된 GUID 캐시 관리자"""
    
    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._data: dict = {"processed_guids": [], "last_updated": None}
        self._load()
    
    def _load(self) -> None:
        """캐시 파일을 로드한다."""
        if self.cache_path.exists():
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.info(f"캐시 로드 완료: {len(self._data.get('processed_guids', []))}개 항목")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"캐시 로드 실패, 초기화: {e}")
                self._data = {"processed_guids": [], "last_updated": None}
        else:
            logger.info("캐시 파일 없음, 새로 생성")
    
    def save(self) -> None:
        """캐시를 파일에 저장한다."""
        self._data["last_updated"] = datetime.now().isoformat()
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        logger.info("캐시 저장 완료")
    
    def is_processed(self, guid: str) -> bool:
        """해당 GUID가 이미 처리되었는지 확인한다."""
        return guid in self._data.get("processed_guids", [])
    
    def mark_processed(self, guid: str) -> None:
        """해당 GUID를 처리 완료로 표시한다."""
        if guid not in self._data["processed_guids"]:
            self._data["processed_guids"].append(guid)


def generate_guid_fallback(title: str, published: Optional[str]) -> str:
    """
    GUID가 없을 경우 fallback ID를 생성한다.
    title + published의 해시값을 사용한다.
    """
    content = f"{title}:{published or 'unknown'}"
    return hashlib.sha256(content.encode()).hexdigest()[:32]


@with_retry(max_attempts=3, base_delay=1.0)
def fetch_feed(url: str) -> feedparser.FeedParserDict:
    """
    RSS 피드를 가져온다.
    
    Args:
        url: RSS 피드 URL
    
    Returns:
        파싱된 피드 데이터
    
    Raises:
        RetryableError: 네트워크 오류 시
    """
    logger.info(f"피드 수집 시작: {url}")
    
    try:
        feed = feedparser.parse(url)
        
        # 피드 오류 확인
        if feed.bozo and feed.bozo_exception:
            error = feed.bozo_exception
            logger.warning(f"피드 파싱 경고: {error}")
        
        if not feed.entries:
            logger.warning(f"피드에 항목이 없음: {url}")
        else:
            logger.info(f"피드 수집 완료: {len(feed.entries)}개 항목")
        
        return feed
    
    except Exception as e:
        logger.error(f"피드 수집 실패: {url}, 오류: {e}")
        raise RetryableError(f"피드 수집 실패: {e}") from e


def parse_feed_item(entry: dict, category: str, source_name: str) -> RSSItem:
    """
    피드 항목을 RSSItem으로 변환한다.
    
    Args:
        entry: feedparser 항목
        category: 카테고리 (column/tips)
        source_name: 소스 이름
    
    Returns:
        RSSItem 인스턴스
    """
    # GUID 추출 (없으면 fallback)
    guid = entry.get("id") or entry.get("guid")
    title = entry.get("title", "제목 없음")
    published = entry.get("published") or entry.get("updated")
    
    if not guid:
        guid = generate_guid_fallback(title, published)
        logger.debug(f"GUID fallback 생성: {guid[:16]}...")
    
    return RSSItem(
        guid=guid,
        title=title,
        link=entry.get("link", ""),
        summary=entry.get("summary", entry.get("description", "")),
        published=published,
        category=category,
        source_name=source_name,
    )


class RSSFetcher:
    """RSS 피드 수집기"""
    
    def __init__(self, config_path: Path, cache_path: Path):
        self.config_path = config_path
        self.cache = CacheManager(cache_path)
        self._config: dict = {}
        self._load_config()
    
    def _load_config(self) -> None:
        """설정 파일을 로드한다."""
        with open(self.config_path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f)
        logger.info("설정 로드 완료")
    
    def get_feeds_by_category(self, category: str) -> List[dict]:
        """특정 카테고리의 피드 설정을 반환한다."""
        feeds = self._config.get("rss_feeds", [])
        return [f for f in feeds if f.get("category") == category]
    
    def fetch_new_items(self, category: str, max_items: int = 1) -> List[RSSItem]:
        """
        새로운 (처리되지 않은) RSS 항목을 가져온다.
        
        Args:
            category: 카테고리 (column/tips)
            max_items: 최대 항목 수
        
        Returns:
            새로운 RSSItem 리스트
        """
        feeds = self.get_feeds_by_category(category)
        
        if not feeds:
            logger.warning(f"카테고리 '{category}'에 해당하는 피드 없음")
            return []
        
        new_items: List[RSSItem] = []
        
        for feed_config in feeds:
            url = feed_config["url"]
            source_name = feed_config.get("name", url)
            
            try:
                feed = fetch_feed(url)
                
                for entry in feed.entries:
                    item = parse_feed_item(entry, category, source_name)
                    
                    # 중복 확인
                    if self.cache.is_processed(item.guid):
                        logger.debug(f"이미 처리된 항목 스킵: {item.title[:30]}...")
                        continue
                    
                    new_items.append(item)
                    
                    if len(new_items) >= max_items:
                        break
                
                if len(new_items) >= max_items:
                    break
                    
            except Exception as e:
                logger.error(f"피드 처리 실패: {url}, 오류: {e}")
                continue
        
        logger.info(f"새 항목 {len(new_items)}개 발견 (카테고리: {category})")
        return new_items[:max_items]
    
    def mark_as_processed(self, item: RSSItem) -> None:
        """항목을 처리 완료로 표시한다."""
        self.cache.mark_processed(item.guid)
        logger.info(f"처리 완료 표시: {item.title[:30]}...")
    
    def save_cache(self) -> None:
        """캐시를 저장한다."""
        self.cache.save()

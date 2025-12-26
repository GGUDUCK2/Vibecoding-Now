"""
콘텐츠 생성 모듈

Gemini API를 사용하여 블로그 콘텐츠를 생성한다.
- 카테고리별 프롬프트 템플릿
- AI 페르소나: 시니어 개발자 + 기획자
- 단순 요약 금지, 전문가 인사이트 포함
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import google.generativeai as genai
import yaml

from retry import NonRetryableError, RetryableError, with_retry
from rss_fetcher import RSSItem

logger = logging.getLogger(__name__)


@dataclass
class GeneratedContent:
    """생성된 블로그 콘텐츠"""
    title: str
    content: str
    labels: list
    source_item: RSSItem


# 프롬프트 템플릿
COLUMN_PROMPT_TEMPLATE = """## 역할
당신은 10년차 시니어 개발자이자 20년차 기획/설계 경험자입니다.
"바이브코딩 Now" 블로그의 전문 컬럼니스트로서 AI와 개발 트렌드를 분석합니다.

## 목표
아래 뉴스/기사를 기반으로 전문가 시각의 컬럼을 작성하세요.

## 요구사항
1. **단순 요약 금지**: 뉴스 내용을 그대로 옮기지 마세요
2. **전문가 인사이트**: 기술적 맥락과 업계 영향을 분석하세요
3. **실행 가이드**: 독자가 실제로 활용할 수 있는 조언을 포함하세요
4. **바이브 코딩 관점**: AI 활용 개발에 대한 시사점을 도출하세요

## 형식
- 제목: 흥미롭고 SEO 친화적인 한국어 제목
- 본문: 1500-2500자
- HTML 형식 (블로거 호환)
- 적절한 소제목(h2, h3) 사용
- 중요 포인트 강조(bold)

## 원본 정보
제목: {title}
링크: {link}
요약: {summary}
출처: {source}

## 출력 형식
다음 형식으로 출력하세요:
---TITLE---
[생성된 제목]
---CONTENT---
[HTML 형식의 본문]
"""

TIPS_CONCEPT_PROMPT_TEMPLATE = """## 역할
당신은 비전공자를 위한 친근한 개발 멘토입니다.
"바이브코딩 Now" 블로그에서 AI 시대의 개발 개념을 쉽게 설명합니다.

## 목표
아래 주제를 바탕으로 비전공 개발자가 이해하기 쉬운 개념 설명 글을 작성하세요.

## 요구사항
1. **쉬운 설명**: 전문 용어는 반드시 풀어서 설명
2. **실제 비유**: 일상생활 비유로 개념 전달
3. **핵심 정리**: 꼭 알아야 할 포인트 명확히
4. **바이브 코딩 연결**: AI 도구 활용과 연결

## 형식
- 제목: 초보자도 클릭하고 싶은 제목
- 본문: 1000-1500자
- HTML 형식
- 단계별 설명 선호
- 핵심 요약 박스 포함

## 참고 정보
제목: {title}
링크: {link}
요약: {summary}
출처: {source}

## 출력 형식
---TITLE---
[생성된 제목]
---CONTENT---
[HTML 형식의 본문]
"""

TIPS_PRACTICAL_PROMPT_TEMPLATE = """## 역할
당신은 AI 도구 활용의 달인입니다.
"바이브코딩 Now" 블로그에서 실전 바이브 코딩 팁을 공유합니다.

## 목표
아래 정보를 바탕으로 즉시 적용 가능한 AI 활용 팁을 작성하세요.

## 요구사항
1. **실전 중심**: 이론보다 바로 써먹을 수 있는 팁
2. **단계별 가이드**: 따라하기 쉬운 스텝
3. **프롬프트 예시**: 실제 사용 가능한 프롬프트 제공
4. **주의사항**: 흔한 실수와 해결법 포함

## 형식
- 제목: 액션 지향적 제목 ("~하는 법", "~팁")
- 본문: 1200-1800자
- HTML 형식
- 코드/프롬프트 블록 포함
- 체크리스트 형태 권장

## 참고 정보
제목: {title}
링크: {link}
요약: {summary}
출처: {source}

## 출력 형식
---TITLE---
[생성된 제목]
---CONTENT---
[HTML 형식의 본문]
"""


def get_prompt_template(category: str) -> str:
    """카테고리에 맞는 프롬프트 템플릿을 반환한다."""
    templates = {
        "column": COLUMN_PROMPT_TEMPLATE,
        "tips-concept": TIPS_CONCEPT_PROMPT_TEMPLATE,
        "tips-practical": TIPS_PRACTICAL_PROMPT_TEMPLATE,
    }
    return templates.get(category, COLUMN_PROMPT_TEMPLATE)


def get_label_for_category(category: str) -> list:
    """카테고리에 맞는 라벨을 반환한다."""
    if category == "column":
        return ["컬럼"]
    return ["Tips"]


def parse_generated_response(response_text: str) -> tuple[str, str]:
    """
    생성된 응답에서 제목과 본문을 추출한다.
    
    Returns:
        (title, content) 튜플
    """
    title = ""
    content = ""
    
    if "---TITLE---" in response_text and "---CONTENT---" in response_text:
        parts = response_text.split("---CONTENT---")
        if len(parts) >= 2:
            title_part = parts[0].replace("---TITLE---", "").strip()
            content = parts[1].strip()
            title = title_part.strip()
    else:
        # fallback: 첫 줄을 제목으로 사용
        lines = response_text.strip().split("\n")
        if lines:
            title = lines[0].strip().replace("#", "").strip()
            content = "\n".join(lines[1:]).strip()
    
    return title, content


class ContentGenerator:
    """콘텐츠 생성기"""
    
    def __init__(self, config_path: Path, api_key: str):
        self.config_path = config_path
        self._config: dict = {}
        self._load_config()
        
        # Gemini 설정
        genai.configure(api_key=api_key)
        
        gemini_config = self._config.get("gemini", {})
        self.model_name = gemini_config.get("model", "gemini-1.5-flash")
        self.max_output_tokens = gemini_config.get("max_output_tokens", 4096)
        self.temperature = gemini_config.get("temperature", 0.7)
        
        self.model = genai.GenerativeModel(
            model_name=self.model_name,
            generation_config={
                "max_output_tokens": self.max_output_tokens,
                "temperature": self.temperature,
            }
        )
        logger.info(f"Gemini 모델 초기화: {self.model_name}")
    
    def _load_config(self) -> None:
        """설정 파일을 로드한다."""
        with open(self.config_path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f)
    
    @with_retry(max_attempts=3, base_delay=2.0)
    def generate(self, item: RSSItem, category: str) -> Optional[GeneratedContent]:
        """
        RSS 항목을 기반으로 블로그 콘텐츠를 생성한다.
        
        Args:
            item: RSS 항목
            category: 콘텐츠 카테고리 (column/tips-concept/tips-practical)
        
        Returns:
            GeneratedContent 또는 None
        """
        logger.info(f"콘텐츠 생성 시작: {item.title[:30]}... (카테고리: {category})")
        
        # 프롬프트 구성
        template = get_prompt_template(category)
        prompt = template.format(
            title=item.title,
            link=item.link,
            summary=item.summary[:500] if item.summary else "요약 없음",
            source=item.source_name,
        )
        
        try:
            response = self.model.generate_content(prompt)
            
            if not response.text:
                logger.warning("Gemini 응답이 비어있음")
                raise RetryableError("빈 응답")
            
            # 응답 파싱
            title, content = parse_generated_response(response.text)
            
            if not title or not content:
                logger.warning("제목 또는 본문 파싱 실패")
                raise RetryableError("파싱 실패")
            
            labels = get_label_for_category(category)
            
            logger.info(f"콘텐츠 생성 완료: {title[:30]}...")
            
            return GeneratedContent(
                title=title,
                content=content,
                labels=labels,
                source_item=item,
            )
        
        except genai.types.BlockedPromptException as e:
            logger.error(f"프롬프트 차단됨: {e}")
            raise NonRetryableError(f"프롬프트 차단: {e}") from e
        
        except genai.types.StopCandidateException as e:
            logger.error(f"생성 중단됨: {e}")
            raise NonRetryableError(f"생성 중단: {e}") from e
        
        except Exception as e:
            logger.error(f"콘텐츠 생성 실패: {e}")
            raise RetryableError(f"생성 실패: {e}") from e

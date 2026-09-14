"""KOSHA(안전보건공단) 안전보건법령 스마트검색 API 클라이언트.

공공데이터포털 15123696 (개발계정 일 10,000건).
    https://apis.data.go.kr/B552468/srch/smartSearch

[검색 특성 — 실측 결과]
- 조문 번호로는 검색되지 않는다. "제43조" → 0건. 본문 키워드만 인덱싱되어 있다.
  따라서 Gemini가 만든 legal_ref 를 이 API로 검증할 수 없다.
  반대로 KOSHA에서 조문을 먼저 가져와 Gemini 응답에 주입하는 방향으로 쓴다.
- 긴 자연어 질의는 매칭률이 급락한다. "개구부 추락 방지" → 0건 / "개구부" → 3건.
  반드시 짧은 명사 키워드로 질의할 것.
- score 정렬만으로는 시행령 별표가 상위를 차지한다. 현장 조치 조문이 모여 있는
  '산업안전보건기준에 관한 규칙'(category 4)을 우선하도록 재정렬한다.
"""

import json
import os
import pathlib
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("KOSHA_API_KEY")
SMART_SEARCH_URL = "https://apis.data.go.kr/B552468/srch/smartSearch"
TIMEOUT = 3.0

CATEGORY = {
    "1": "산업안전보건법",
    "2": "산업안전보건법 시행령",
    "3": "산업안전보건법 시행규칙",
    "4": "산업안전보건기준에 관한 규칙",
    "5": "고시·훈령·작업지침",
    "6": "안전보건 자료",
    "7": "KOSHA GUIDE",
    "8": "중대재해처벌법",
    "9": "중대재해처벌법 시행령",
    "11": "기타 법령",
}

# 법령 조문 카테고리 — 현장 적용도가 높은 순. 정렬 우선순위로도 쓴다.
LAW_PRIORITY = ["4", "1", "3", "8", "2", "9", "11"]
GUIDE_CATEGORY = 7
MEDIA_CATEGORY = 6  # 유일하게 filepath(포털 원문 URL)를 제공

# 위험코드 → 표시용 한글명. 아래 RISK_CODE_QUERY/RISK_CODE_MEDIA 와 같은 키를 쓴다.
RISK_CODE_KR = {
    "FALL_RISK": "추락 위험",
    "PPE_HELMET_MISSING": "안전모 미착용",
    "OPENING_UNPROTECTED": "개구부 방호 미흡",
    "ELECTRIC_RISK": "감전 위험",
    "LOAD_UNSTABLE": "적재 불량 / 낙하 위험",
}

# 위험코드 → 검색 설정. 실제 호출로 조문 적합도를 눈으로 확인해 고른 값이다.
#   queries   : 검색 키워드. 한 단어로 핵심 조문이 안 잡히면 여러 개를 쓴다.
#   per_query : 키워드 하나당 채택할 조문 수. 여러 키워드를 쓸 때 1로 두어야
#               첫 키워드가 자리를 다 먹지 않는다.
#   prefer    : score 정렬이 엉뚱한 조문을 올릴 때 끌어올릴 제목 조각.
#               (예: '낙하물' 은 제198조 낙하물 보호구조(차량계 하역기계용)가
#                1위로 오지만, 건설현장에는 제14조가 맞다)
RISK_CODE_QUERY = {
    # '안전난간'/'작업발판' 은 제목만 그럴듯하고 본문이 궤도작업차량(제410조)·
    # 기계 조작높이(제9조) 조문이라 건설현장에 맞지 않았다. '비계' 가 정확하다.
    "FALL_RISK": {"queries": ["비계"], "per_query": 2, "prefer": ["작업발판의 구조"]},
    "PPE_HELMET_MISSING": {"queries": ["안전모"], "per_query": 2},
    "OPENING_UNPROTECTED": {"queries": ["개구부"], "per_query": 1},
    "ELECTRIC_RISK": {"queries": ["감전", "충전전로"], "per_query": 1},
    "LOAD_UNSTABLE": {
        "queries": ["적재", "낙하물"],
        "per_query": 1,
        "prefer": ["낙하물에 의한"],
    },
}

# 위험코드 → 안전보건 자료(category 6) 검색 설정. 이 카테고리만 포털 원문 URL을
# 제공하므로 카카오 textCard 의 [원문 보기] 버튼은 여기서 나온다.
#   prefer : 같은 키워드라도 건설현장에 쓸모 있는 자료를 앞으로 끌어올린다.
RISK_CODE_MEDIA = {
    "FALL_RISK": {"query": "추락", "prefer": ["건설현장"]},
    "PPE_HELMET_MISSING": {"query": "안전모", "prefer": ["길잡이"]},
    "OPENING_UNPROTECTED": {"query": "개구부", "prefer": ["덮개"]},
    "ELECTRIC_RISK": {"query": "전기작업", "prefer": ["안전기준"]},
    "LOAD_UNSTABLE": {"query": "적재", "prefer": ["가이드"]},
}

# 위험코드별 조문·자료는 고정이라 사전 조회해 캐시로 둔다 (scripts/build_kosha_cache.py).
# 런타임 API 호출 없이 응답하므로 지연 0, 트래픽 0, 포털 장애와 무관하다.
CACHE_PATH = pathlib.Path(__file__).parent / "data" / "kosha_cache.json"

_HIGHLIGHT_RE = re.compile(r"</?em[^>]*>")
# 조문 본문에 섞여 있는 편집 표기(<개정 2012. 3. 5.>, <신설 ...>). 인용에는 불필요하다.
_AMEND_RE = re.compile(r"<\s*(?:개정|신설|전문개정|본조신설|제목개정)[^>]*>")


async def search(query: str, category: int = 0, rows: int = 20) -> list[dict]:
    """스마트검색 1회 호출. 실패하면 빈 리스트 — 챗봇 응답을 막지 않는다."""
    if not API_KEY or not query:
        return []

    params = {
        "serviceKey": API_KEY,
        "pageNo": 1,
        "numOfRows": rows,
        "searchValue": query,
        "category": category,
        "_type": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(SMART_SEARCH_URL, params=params)
            resp.raise_for_status()
            body = resp.json()["response"]["body"]
    except Exception:
        return []

    items = body.get("items")
    raw = items.get("item", []) if isinstance(items, dict) else []
    return [_normalize(x) for x in raw]


def _clean(text: str | None) -> str:
    return " ".join(_AMEND_RE.sub("", text or "").split())


def _normalize(item: dict) -> dict:
    cat = str(item.get("category", ""))
    return {
        "category": cat,
        "source": CATEGORY.get(cat, "안전보건 자료"),
        "title": (item.get("title") or "").strip(),
        "content": _clean(item.get("content")),
        "excerpt": _HIGHLIGHT_RE.sub("", item.get("highlight_content") or "").strip(),
        "url": item.get("filepath") or "",
        "score": item.get("score", 0),
    }


async def find_laws(query: str, limit: int = 2) -> list[dict]:
    """법령 조문 조회. category=0 으로 한 번만 호출하고 클라이언트에서 재정렬한다."""
    hits = await search(query, category=0, rows=20)
    laws = [h for h in hits if h["category"] in LAW_PRIORITY]
    laws.sort(key=lambda h: (LAW_PRIORITY.index(h["category"]), -h["score"]))
    return laws[:limit]


async def fetch_laws_for_code(risk_code: str, limit: int = 2) -> list[dict]:
    """위험코드에 해당하는 법령 조문을 API에서 조회. 캐시 생성용이며 런타임에는 쓰지 않는다."""
    cfg = RISK_CODE_QUERY.get(risk_code)
    if not cfg:
        return []

    per_query = cfg.get("per_query", 1)
    prefer = cfg.get("prefer", [])
    merged: list[dict] = []
    seen: set[str] = set()

    for query in cfg["queries"]:
        hits = await find_laws(query, limit=10)
        # prefer 에 걸리는 조문을 앞으로 끌어올린다 (없으면 원래 순서 유지).
        hits.sort(key=lambda h: not any(p in h["title"] for p in prefer))
        for law in hits[:per_query]:
            if law["title"] not in seen:
                seen.add(law["title"])
                merged.append(law)

    return merged[:limit]


async def fetch_media_for_code(risk_code: str) -> dict | None:
    """위험코드에 해당하는 안전보건 자료를 API에서 조회. 캐시 생성용."""
    cfg = RISK_CODE_MEDIA.get(risk_code)
    if not cfg:
        return None

    hits = [h for h in await search(cfg["query"], category=MEDIA_CATEGORY, rows=10) if h["url"]]
    prefer = cfg.get("prefer", [])
    hits.sort(key=lambda h: not any(p in h["title"] for p in prefer))
    return hits[0] if hits else None


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        # 배포 번들에서 data/ 가 빠지면 조문·링크가 조용히 사라져 원인 추적이 어렵다.
        # vercel.json 의 includeFiles 설정을 확인할 것.
        print(f"[KOSHA] 캐시 로드 실패 ({CACHE_PATH}): {e}")
        return {}


_CACHE = _load_cache()


def laws_for_code(risk_code: str) -> list[dict]:
    """위험코드에 해당하는 법령 조문 (캐시). 캐시가 없으면 빈 리스트."""
    return _CACHE.get("codes", {}).get(risk_code, [])


def media_for_code(risk_code: str) -> dict | None:
    """위험코드에 해당하는 안전보건 자료 (캐시). title/url 을 가진다."""
    return _CACHE.get("media", {}).get(risk_code)


async def find_guide(query: str) -> dict | None:
    """KOSHA GUIDE 기술지침 1건."""
    hits = await search(query, category=GUIDE_CATEGORY, rows=1)
    return hits[0] if hits else None


async def find_media(query: str) -> dict | None:
    """포털 원문 링크가 있는 안전보건 자료 1건."""
    hits = await search(query, category=MEDIA_CATEGORY, rows=1)
    return hits[0] if hits else None


def format_citation(law: dict, max_len: int = 200) -> str:
    """카카오 말풍선용 조문 인용 블록."""
    body = law["content"]
    if len(body) > max_len:
        body = body[:max_len].rstrip() + "…"
    return f"📜 {law['source']} {law['title']}\n   “{body}”"

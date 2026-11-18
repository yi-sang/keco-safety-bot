import os
import io
import re
import httpx
import json
from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv

import kosha

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# 프리뷰 모델은 예고 없이 내려간다(gemini-3-flash-preview 가 404 로 사라져
# 세 기능이 모두 멈췄다). GA 모델을 기본으로 쓰고, 다음에 같은 일이 생기면
# 재배포 없이 Vercel 환경변수 GEMINI_MODEL 로 갈아끼울 수 있게 둔다.
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

PROMPT = """[IMPORTANT] Respond in JSON only.

당신은 건설/공사현장 안전 전문가입니다. KOSHA(안전보건공단) 가이드와 산업안전보건법을 기반으로 분석하세요.
아래 사진을 분석하여 위험요소를 파악하세요.

[위험코드 목록]
- FALL_RISK: 추락 위험 (고소작업, 난간 미설치, 작업발판 불안정)
- PPE_HELMET_MISSING: 안전모 미착용
- OPENING_UNPROTECTED: 개구부 방호 미흡 (덮개/난간 부족)
- ELECTRIC_RISK: 감전 위험 (노출 전선, 임시 배선, 분전반)
- LOAD_UNSTABLE: 적재 불량 / 낙하 위험

[지시사항]
1. 사진에서 보이는 사실만 기술하세요.
2. 위 위험코드 중 해당하는 것을 분류하세요. (확실하지 않으면 confidence 낮게)
3. 각 위험요소에 대해 아래를 모두 작성하세요:
   - 위험도(상/중/하)
   - 위험 판단 근거 (사진에서 관찰된 사실 기준)
   - 즉시 조치사항 (구체적으로 2~3가지)
4. 종합 위험도 평가를 작성하세요.
5. 법령 조문(제○조)이나 KOSHA GUIDE 번호를 직접 쓰지 마세요.
   관련 법령은 위험코드에 따라 시스템이 KOSHA 공식 데이터에서 붙입니다.
6. 반드시 아래 JSON 형식으로만 응답하세요.

{
  "scene_summary": "현장 상황 요약 (2~3문장)",
  "hazards": [
    {
      "code": "위험코드",
      "confidence": 0.0,
      "risk_level": "상/중/하",
      "reason": "위험 판단 근거",
      "action": "즉시 조치사항"
    }
  ],
  "overall_risk": "종합 위험도 평가 한 문장",
  "uncertainty_note": "불확실한 부분 (없으면 빈 문자열)"
}
"""

RISK_LEVEL_EMOJI = {"상": "🔴", "중": "🟠", "하": "🟡"}

# 카카오 simpleText 는 1000자 제한. 조문 전문을 그대로 붙이면 넘치므로 예산을 둔다.
KAKAO_TEXT_LIMIT = 1000
LAW_PER_HAZARD = 1
LAW_BODY_LEN = 90

# 위험요소를 전부 상세히 쓰면 4건째부터 1000자를 넘겨 조문 인용이 통째로
# 잘려나간다. 위험도·confidence 상위 몇 건만 상세히 쓰고 나머지는 한 줄로
# 요약해, 조문 원문을 항상 살리고 조치 우선순위도 드러나게 한다.
# 요약된 위험도 KOSHA 자료 링크(listCard)에는 그대로 포함된다.
MAX_DETAILED_HAZARDS = 3
RISK_LEVEL_ORDER = {"상": 0, "중": 1, "하": 2}

RISK_CODE_KR = kosha.RISK_CODE_KR


async def analyze_image(image_url: str) -> tuple[str, list[str]]:
    """이미지 URL을 분석해 (카카오 응답 텍스트, 감지된 위험코드 목록) 반환.

    위험코드는 응답에 붙일 KOSHA 자료 링크(textCard)를 고르는 데 쓴다.
    """
    # 이미지 다운로드
    async with httpx.AsyncClient(timeout=10.0) as http_client:
        resp = await http_client.get(image_url)
        resp.raise_for_status()
        image_bytes = resp.content

    # 이미지 압축 (800px로 리사이즈)
    img = Image.open(io.BytesIO(image_bytes))
    img.thumbnail((800, 800))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    image_bytes = buf.getvalue()

    # Gemini 호출 (새 SDK)
    image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
    response = await client.aio.models.generate_content(
        model=MODEL_NAME,
        contents=[PROMPT, image_part]
    )

    result = _parse_json(response.text)

    codes = [h.get("code", "") for h in _sort_hazards(result.get("hazards", []))]
    return _format_result(result), codes


def _sort_hazards(hazards: list[dict]) -> list[dict]:
    """위험도(상>중>하) 우선, 같으면 confidence 높은 순."""
    def key(h: dict):
        level = RISK_LEVEL_ORDER.get(h.get("risk_level"), 9)
        try:
            conf = float(h.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        return (level, -conf)

    return sorted(hazards, key=key)


def _format_result(result: dict) -> str:
    """Gemini 분석 결과를 카카오 응답 텍스트로 포맷"""
    lines = ["📋 현장 위험요소 분석 결과\n"]
    cited = False

    summary = result.get("scene_summary", "")
    if summary:
        lines.append(f"📍 현장 상황: {summary}\n")

    hazards = _sort_hazards(result.get("hazards", []))
    if not hazards:
        lines.append("위험요소가 감지되지 않았습니다.")
    else:
        for i, h in enumerate(hazards[:MAX_DETAILED_HAZARDS], 1):
            code = h.get("code", "")
            name = RISK_CODE_KR.get(code, code)
            level = h.get("risk_level", "하")
            emoji = RISK_LEVEL_EMOJI.get(level, "")
            lines.append(f"{i}. {emoji} {name} [{level}위험]")
            lines.append(f"   📌 근거: {h.get('reason', '')}")
            lines.append(f"   🔧 조치: {h.get('action', '')}")
            for law in kosha.laws_for_code(code)[:LAW_PER_HAZARD]:
                lines.append(f"   📜 {law['source']} {law['title']}")
                lines.append(f"      “{_clip(law['content'], LAW_BODY_LEN)}”")
                cited = True
            lines.append("")

        rest = hazards[MAX_DETAILED_HAZARDS:]
        if rest:
            labels = ", ".join(
                f"{RISK_CODE_KR.get(h.get('code', ''), h.get('code', ''))}[{h.get('risk_level', '')}]"
                for h in rest
            )
            lines.append(f"※ 그 외 {len(rest)}건 감지: {labels}\n")

    overall = result.get("overall_risk", "")
    if overall:
        lines.append(f"⚠️ 종합평가: {overall}\n")

    uncertainty = result.get("uncertainty_note", "")
    if uncertainty:
        lines.append(f"※ 불확실 사항: {uncertainty}")

    lines.append("※ 현장 점검으로 최종 확인 필요")
    if cited:
        lines.append("📡 법령 출처: KOSHA 안전보건공단 안전보건법령 스마트검색")
    return _fit(lines)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# 1000자를 넘길 때 버리는 줄의 접두사 — 앞에서부터 차례로 덜어낸다.
# 인용문(본문) → 조문 제목 순으로, 정보 손실이 적은 쪽부터 버린다.
DROPPABLE_PREFIXES = ("      “", "   “", "   📜 ", "   └ ", "• ")


def _fit(lines: list[str], droppable: tuple[str, ...] = DROPPABLE_PREFIXES) -> str:
    """카카오 1000자 제한에 맞춘다. 넘치면 조문 인용부터 덜어낸다."""
    text = "\n".join(lines)
    if len(text) <= KAKAO_TEXT_LIMIT:
        return text

    for prefix in droppable:
        lines = [ln for ln in lines if not ln.startswith(prefix)]
        text = "\n".join(lines)
        if len(text) <= KAKAO_TEXT_LIMIT:
            return text

    return text[:KAKAO_TEXT_LIMIT - 1] + "…"


# ── 모델이 지어낸 인용 차단 ────────────────────────────────
#
# 질문·체크리스트는 사진분석과 달리 위험코드가 없어 캐시된 조문을 붙일 수 없다.
# 예전에는 프롬프트가 모델에게 직접 "KOSHA GUIDE 번호"를 쓰게 했는데, 번호는
# 모델 기억에서 나오므로 실제 지침과 어긋났다(C-32 는 '추락재해방지 표준작업지침'이
# 아니라 '시스템 비계 안전작업 지침'이다). 또 스마트검색 API 는 지침 번호를
# 제공하지 않아 번호는 검증할 방법 자체가 없다.
#
# 그래서 (1) 모델은 번호를 쓰지 못하게 하고, (2) 근거는 KOSHA 검색으로 실제
# 조회된 것만 코드가 붙이며, (3) 그래도 새어 나온 번호는 아래에서 지운다.
# "및 KOSHA GUIDE C-32(추락재해방지 표준작업지침)" 처럼 앞의 연결어미와 뒤의
# 괄호 부제까지 함께 지운다. 번호만 떼면 "및 (추락재해방지…)" 같은 조각이 남는다.
GUIDE_NO_RE = re.compile(
    r"(?:\s*(?:및|와|과|,))?\s*"
    r"(?:\(?\s*(?:KOSHA\s*GUIDE|코샤\s*가이드)\s*[A-Z]\s*-\s*\d+(?:\s*-\s*\d{2,4})?\s*\)?"
    r"|\(\s*[A-Z]\s*-\s*\d+(?:\s*-\s*\d{2,4})?\s*\))"
    r"(?:\s*\([^)]{0,40}\))?",
    re.I,
)
UNVERIFIED_ARTICLE = "관련 조항"

# 질문 답변·체크리스트에 붙일 근거 건수. 말풍선 1000자 안에 들어가야 한다.
QA_LAW_LIMIT = 2
QA_GUIDE_LIMIT = 1
QA_BODY_LEN = 80
CHECK_LAW_LIMIT = 2
CHECK_GUIDE_LIMIT = 1
# 체크리스트 항목별 근거는 조회된 후보 중에서만 고르게 한다.
CHECK_CANDIDATE_LAWS = 6
CHECK_CANDIDATE_GUIDES = 3

OFF_TOPIC_REPLY = "현장 안전 관련 질문만 답변드릴 수 있습니다."
SOURCE_NOTE = "📡 근거 출처: KOSHA 안전보건공단 안전보건법령 스마트검색"


def _parse_json(raw: str) -> dict:
    """Gemini 응답에서 JSON 추출. 마크다운 코드블록으로 감싸도 처리한다."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _scrub(text: str, allowed: set[str]) -> str:
    """조회로 확인되지 않은 조문 번호·지침 번호를 문장에서 제거한다."""
    text = GUIDE_NO_RE.sub("", text or "")

    def keep_or_blur(m: re.Match) -> str:
        return m.group(0) if m.group(0).replace(" ", "") in allowed else UNVERIFIED_ARTICLE

    return " ".join(kosha.ARTICLE_RE.sub(keep_or_blur, text).split())


def _ref_label(ref: dict) -> str:
    """근거 한 줄. KOSHA GUIDE 는 번호 없이 지침명으로만 쓴다."""
    return f"{ref['source']} {ref['title']}".strip()


# ── 현장 안전 질문 ──────────────────────────────────────────

SAFETY_QA_PROMPT = """[IMPORTANT] Respond in JSON only.

당신은 건설/공사현장 안전 전문가입니다. KOSHA(안전보건공단) 가이드와 산업안전보건법을 기반으로 답변하세요.

질문: {question}

[답변 규칙]
1. 법령 조문 번호(제○조)나 KOSHA GUIDE 번호(C-○○ 등)를 절대 쓰지 마세요.
   기억에 의존한 번호는 실제 조문·지침과 어긋납니다.
   관련 근거는 시스템이 KOSHA 공식 검색 결과로 붙입니다.
2. answer 는 질문에 대한 핵심 답변 2~3문장, 합계 250자 이내.
3. actions 는 현장에서 즉시 실행 가능한 조치 2~3가지, 각 60자 이내.
4. search_keywords 는 KOSHA 법령·지침 검색어 1~3개. 검색은 조문 제목·본문을
   그대로 매칭하므로, 법령 조문 제목에 가까운 짧은 구로 쓸수록 정확히 걸립니다.
   - 좋음: "안전난간의 구조", "추락의 방지", "개구부 방호", "충전전로", "고소작업대"
   - 나쁨: "안전난간"처럼 너무 뭉뚱그린 말, "안전"·"작업" 같은 일반어, 문장·조사
5. 공사현장 안전과 무관한 질문이면 off_topic 을 true 로 하고 나머지는 비우세요.

{{
  "answer": "핵심 답변",
  "actions": ["조치1", "조치2"],
  "search_keywords": ["키워드1", "키워드2"],
  "off_topic": false
}}
"""

REF_PICK_PROMPT = """[IMPORTANT] Respond in JSON only.

아래 [후보]는 KOSHA 공식 검색으로 실제 조회된 법령 조문과 기술지침입니다.
주제에 직접 해당하는 것만 고르세요. 애매하거나 맞는 것이 없으면 빈 배열로 두세요.
후보에 없는 조문·지침을 새로 만들어 쓰지 마세요.

주제: {topic}

[후보]
{candidates}

{{"law_ids": [법령 번호 최대 {law_limit}개], "guide_ids": [지침 번호 최대 {guide_limit}개]}}
"""

# 체크리스트 항목별 근거는 모델에게 맡기지 않는다. 숫자 매핑을 요청하면
# 딕셔너리든 리스트든 이 필드를 비워 보냈다. 조문 제목의 핵심어가 항목 문장에
# 실제로 들어 있을 때만 코드가 붙인다 — 결정적이고 지어낼 여지가 없다.
# 조문 제목에 흔해서 변별력이 없는 말은 매칭에서 뺀다.
ITEM_MATCH_STOPWORDS = {
    "설치", "사용", "조치", "작업", "안전", "준수", "기준", "관리", "구조",
    "방지", "금지", "점검", "조립", "해체", "변경", "지급", "사항", "시의",
    "등의", "때의", "대한", "관한", "위한",
}
ARTICLE_PREFIX_RE = re.compile(r"^제\s?\d+조(?:의\s?\d+)?\s*")


def _match_item_refs(items: list[dict], refs: list[dict]) -> dict:
    """체크 항목 ↔ 조문 매칭. 조문 제목의 핵심어가 항목에 들어 있을 때만 붙인다."""
    laws = [r for r in refs if r["kind"] == "law"]
    keyed = []
    for law in laws:
        body = ARTICLE_PREFIX_RE.sub("", law["title"])
        words = {w.strip("의·,ㆍ") for w in body.split()}
        keyed.append((law, {w for w in words if len(w) >= 2 and w not in ITEM_MATCH_STOPWORDS}))

    matched = {}
    for i, item in enumerate(items, 1):
        text = item.get("check", "")
        best, score = None, 0
        for law, words in keyed:
            hits = sum(1 for w in words if w in text)
            if hits > score:
                best, score = law, hits
        if best:
            matched[i] = best
    return matched


def _candidate_block(refs: list[dict]) -> str:
    lines = []
    for i, ref in enumerate(refs, 1):
        lines.append(f"{i}. [{ref['source']}] {ref['title']} — {_clip(ref['content'], 70)}")
    return "\n".join(lines)


async def _pick_references(topic: str, refs: list[dict], law_limit: int,
                           guide_limit: int) -> list[dict]:
    """조회된 후보 중 주제에 맞는 것만 모델이 고르게 한다.

    고르기만 하므로 지어낼 여지가 없고, 돌아온 번호도 범위를 검증한다.
    선택 호출이 실패하면 검색 점수 순 상위 몇 건으로 조용히 되돌아간다.
    """
    fallback = ([r for r in refs if r["kind"] == "law"][:law_limit]
                + [r for r in refs if r["kind"] == "guide"][:guide_limit])
    if not refs:
        return []

    prompt = REF_PICK_PROMPT.format(
        topic=topic,
        candidates=_candidate_block(refs),
        law_limit=law_limit,
        guide_limit=guide_limit,
    )

    try:
        response = await client.aio.models.generate_content(model=MODEL_NAME, contents=[prompt])
        picked = _parse_json(response.text)
    except Exception as e:
        print(f"[REF PICK ERROR] {e}")
        return fallback

    def by_ids(ids, kind, limit):
        out = []
        for raw_id in ids or []:
            try:
                ref = refs[int(raw_id) - 1]
            except (ValueError, TypeError, IndexError):
                continue
            if ref["kind"] == kind and ref not in out:
                out.append(ref)
        return out[:limit]

    chosen = by_ids(picked.get("law_ids"), "law", law_limit)
    chosen += by_ids(picked.get("guide_ids"), "guide", guide_limit)
    return chosen or fallback


async def answer_safety_question(question: str) -> str:
    """안전 관련 텍스트 질문에 Gemini가 답변하고, 근거는 KOSHA 검색으로 붙인다."""
    response = await client.aio.models.generate_content(
        model=MODEL_NAME,
        contents=[SAFETY_QA_PROMPT.format(question=question)],
    )
    raw = response.text.strip()

    try:
        result = _parse_json(raw)
    except Exception as e:
        # JSON 이 깨져도 답변 자체는 내보낸다. 단, 검증 안 된 번호는 지운다.
        print(f"[QA PARSE ERROR] {e}")
        return _scrub(raw, set())

    if result.get("off_topic"):
        return OFF_TOPIC_REPLY

    refs = await kosha.gather_references(result.get("search_keywords") or [])
    chosen = await _pick_references(question, refs, QA_LAW_LIMIT, QA_GUIDE_LIMIT)
    allowed = kosha.cited_articles(refs)

    lines = [_scrub(result.get("answer", ""), allowed)]

    actions = [a for a in (result.get("actions") or []) if a]
    if actions:
        lines.append("\n🔧 현장 적용")
        for i, action in enumerate(actions, 1):
            lines.append(f"{i}. {_scrub(action, allowed)}")

    if chosen:
        lines.append("\n📜 관련 근거")
        for ref in chosen:
            lines.append(f"• {_ref_label(ref)}")
            lines.append(f"   “{_clip(ref['content'], QA_BODY_LEN)}”")
        lines.append(SOURCE_NOTE)

    return _fit(lines)


# ── 현장 안전 체크리스트 ────────────────────────────────────

SAFETY_CHECK_PROMPT = """[IMPORTANT] Respond in JSON only.

당신은 건설/공사현장 안전 전문가입니다. KOSHA(안전보건공단) 가이드와 산업안전보건법을 기반으로 작성하세요.

작업 유형: {work_type}

위 작업을 시작하기 전 반드시 확인해야 할 안전 체크리스트를 작성하세요.

[규칙]
1. 체크 항목은 8~10개, 각 항목은 현장에서 즉시 확인 가능한 구체적 내용으로 70자 이내.
2. 법령 조문 번호(제○조)나 KOSHA GUIDE 번호(C-○○ 등)를 절대 쓰지 마세요.
   기억에 의존한 번호는 실제 조문·지침과 어긋납니다.
   적용 기준은 시스템이 KOSHA 공식 검색 결과로 붙입니다.
3. search_keywords 는 KOSHA 법령·지침 검색어 1~3개. 검색은 조문 제목·본문을
   그대로 매칭하므로, 법령 조문 제목에 가까운 짧은 구로 쓸수록 정확히 걸립니다.
   - 좋음: "작업발판의 구조", "고소작업대", "안전대의 부착설비", "충전전로"
   - 나쁨: 너무 뭉뚱그린 말, "안전"·"작업" 같은 일반어, 문장·조사

{{
  "work_type": "작업명",
  "search_keywords": ["키워드1", "키워드2"],
  "items": [
    {{"check": "체크 항목 내용", "category": "PPE/환경/장비/절차 중 하나"}}
  ]
}}
"""

CATEGORY_EMOJI = {
    "PPE": "🦺",
    "환경": "🏗️",
    "장비": "🔧",
    "절차": "📋",
}


async def generate_safety_checklist(work_type: str) -> str:
    """작업 유형에 맞는 안전 체크리스트 생성. 적용 기준은 KOSHA 검색으로 붙인다."""
    response = await client.aio.models.generate_content(
        model=MODEL_NAME,
        contents=[SAFETY_CHECK_PROMPT.format(work_type=work_type)],
    )
    result = _parse_json(response.text)

    items = [it for it in (result.get("items") or []) if it.get("check")]
    refs = await kosha.gather_references(
        result.get("search_keywords") or [work_type],
        law_limit=CHECK_CANDIDATE_LAWS,
        guide_limit=CHECK_CANDIDATE_GUIDES,
    )
    chosen = await _pick_references(
        f"{work_type} 작업 안전 체크리스트", refs, CHECK_LAW_LIMIT, CHECK_GUIDE_LIMIT,
    )
    # 항목별 근거는 선택된 조문뿐 아니라 조회된 후보 전체에서 찾는다.
    return _format_checklist(result, items, chosen, _match_item_refs(items, refs),
                             kosha.cited_articles(refs))


def _format_checklist(result: dict, items: list[dict], refs: list[dict],
                      item_refs: dict, allowed: set[str]) -> str:
    work_type = result.get("work_type", "작업")

    lines = [f"✅ {work_type} 작업 전 안전 체크리스트\n"]

    if refs:
        lines.append("📜 적용 기준: " + " / ".join(_ref_label(r) for r in refs) + "\n")

    for i, item in enumerate(items, 1):
        emoji = CATEGORY_EMOJI.get(item.get("category", ""), "▪️")
        lines.append(f"{i}. {emoji} {_scrub(item.get('check', ''), allowed)}")
        ref = item_refs.get(i)
        if ref:
            lines.append(f"   └ {_ref_label(ref)}")

    lines.append("\n※ 모든 항목 확인 후 작업 시작하세요.")
    if refs:
        lines.append(SOURCE_NOTE)
    return _fit(lines)

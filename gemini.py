import os
import io
import httpx
import json
from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

MODEL_NAME = "gemini-3-flash-preview"

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
   - 위험 판단 근거 (KOSHA 기준 또는 산안법 조항 명시)
   - 즉시 조치사항 (구체적으로 2~3가지)
   - 관련 법령/기준 (예: 산안법 제38조, KOSHA GUIDE C-31)
4. 종합 위험도 평가를 작성하세요.
5. 반드시 아래 JSON 형식으로만 응답하세요.

{
  "scene_summary": "현장 상황 요약 (2~3문장)",
  "hazards": [
    {
      "code": "위험코드",
      "confidence": 0.0,
      "risk_level": "상/중/하",
      "reason": "위험 판단 근거",
      "action": "즉시 조치사항",
      "legal_ref": "관련 법령/KOSHA 기준"
    }
  ],
  "overall_risk": "종합 위험도 평가 한 문장",
  "uncertainty_note": "불확실한 부분 (없으면 빈 문자열)"
}
"""

RISK_LEVEL_EMOJI = {"상": "🔴", "중": "🟠", "하": "🟡"}

RISK_CODE_KR = {
    "FALL_RISK": "추락 위험",
    "PPE_HELMET_MISSING": "안전모 미착용",
    "OPENING_UNPROTECTED": "개구부 방호 미흡",
    "ELECTRIC_RISK": "감전 위험",
    "LOAD_UNSTABLE": "적재 불량 / 낙하 위험",
}


async def analyze_image(image_url: str) -> str:
    """이미지 URL을 받아 Gemini로 분석 후 카카오 응답용 텍스트 반환"""
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

    # JSON 파싱
    raw_text = response.text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
    result = json.loads(raw_text.strip())

    return _format_result(result)


def _format_result(result: dict) -> str:
    """Gemini 분석 결과를 카카오 응답 텍스트로 포맷"""
    lines = ["📋 현장 위험요소 분석 결과\n"]

    summary = result.get("scene_summary", "")
    if summary:
        lines.append(f"📍 현장 상황: {summary}\n")

    hazards = result.get("hazards", [])
    if not hazards:
        lines.append("위험요소가 감지되지 않았습니다.")
    else:
        for i, h in enumerate(hazards, 1):
            code = h.get("code", "")
            name = RISK_CODE_KR.get(code, code)
            level = h.get("risk_level", "하")
            emoji = RISK_LEVEL_EMOJI.get(level, "")
            lines.append(f"{i}. {emoji} {name} [{level}위험]")
            lines.append(f"   📌 근거: {h.get('reason', '')}")
            lines.append(f"   🔧 조치: {h.get('action', '')}")
            if h.get("legal_ref"):
                lines.append(f"   📜 기준: {h.get('legal_ref', '')}")
            lines.append("")

    overall = result.get("overall_risk", "")
    if overall:
        lines.append(f"⚠️ 종합평가: {overall}\n")

    uncertainty = result.get("uncertainty_note", "")
    if uncertainty:
        lines.append(f"※ 불확실 사항: {uncertainty}")

    lines.append("※ 현장 점검으로 최종 확인 필요")
    return "\n".join(lines)


SAFETY_QA_PROMPT = """당신은 건설/공사현장 안전 전문가입니다. KOSHA(안전보건공단) 가이드와 산업안전보건법을 기반으로 답변하세요.

질문: {question}

[답변 규칙]
1. 답변은 아래 구조로 작성하세요:
   - 핵심 답변 (2~3문장)
   - 관련 법령/기준 (산안법 조항 또는 KOSHA GUIDE 번호)
   - 현장 적용 방법 (즉시 실행 가능한 2~3가지)
2. 카카오톡 말풍선 기준 800자 이내로 작성하세요.
3. 공사현장과 무관한 질문은 "현장 안전 관련 질문만 답변드릴 수 있습니다."라고만 답변하세요.
"""

SAFETY_CHECK_PROMPT = """당신은 건설/공사현장 안전 전문가입니다. KOSHA(안전보건공단) 가이드와 산업안전보건법을 기반으로 작성하세요.

작업 유형: {work_type}

위 작업을 시작하기 전 반드시 확인해야 할 안전 체크리스트를 작성하세요.

[규칙]
1. 체크 항목은 8~10개로 작성하세요.
2. 각 항목은 현장에서 즉시 확인 가능한 구체적인 내용으로 작성하세요.
3. 가능한 경우 관련 법령/KOSHA 기준을 legal_ref에 명시하세요.
4. 반드시 아래 JSON 형식으로만 응답하세요.

{{
  "work_type": "작업명",
  "legal_basis": "주요 적용 법령/기준 (예: 산안법 제○조, KOSHA GUIDE)",
  "items": [
    {{"check": "체크 항목 내용", "category": "PPE/환경/장비/절차 중 하나", "legal_ref": "관련 법령 (없으면 빈 문자열)"}}
  ]
}}
"""

CATEGORY_EMOJI = {
    "PPE": "🦺",
    "환경": "🏗️",
    "장비": "🔧",
    "절차": "📋",
}


async def answer_safety_question(question: str) -> str:
    """안전 관련 텍스트 질문에 Gemini가 답변"""
    prompt = SAFETY_QA_PROMPT.format(question=question)
    response = await client.aio.models.generate_content(
        model=MODEL_NAME,
        contents=[prompt]
    )
    return response.text.strip()


async def generate_safety_checklist(work_type: str) -> str:
    """작업 유형에 맞는 안전 체크리스트 생성"""
    prompt = SAFETY_CHECK_PROMPT.format(work_type=work_type)
    response = await client.aio.models.generate_content(
        model=MODEL_NAME,
        contents=[prompt]
    )

    raw_text = response.text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]

    result = json.loads(raw_text.strip())
    return _format_checklist(result)


def _format_checklist(result: dict) -> str:
    work_type = result.get("work_type", "작업")
    items = result.get("items", [])
    legal_basis = result.get("legal_basis", "")

    lines = [f"✅ {work_type} 작업 전 안전 체크리스트\n"]

    if legal_basis:
        lines.append(f"📜 적용 기준: {legal_basis}\n")

    for i, item in enumerate(items, 1):
        emoji = CATEGORY_EMOJI.get(item.get("category", ""), "▪️")
        lines.append(f"{i}. {emoji} {item.get('check', '')}")
        if item.get("legal_ref"):
            lines.append(f"   └ {item.get('legal_ref', '')}")

    lines.append("\n※ 모든 항목 확인 후 작업 시작하세요.")
    return "\n".join(lines)

import os
import httpx
import json
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

MODEL_NAME = "gemini-3-pro-image-preview"

PROMPT = """
당신은 공사현장 안전 전문가입니다.
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
3. 각 위험요소에 대해 위험도(상/중/하)와 즉시 조치사항을 작성하세요.
4. 반드시 아래 JSON 형식으로만 응답하세요.

{
  "scene_summary": "현장 상황 요약",
  "hazards": [
    {
      "code": "위험코드",
      "confidence": 0.0~1.0,
      "risk_level": "상/중/하",
      "reason": "위험 판단 근거",
      "action": "즉시 조치사항"
    }
  ],
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
        content_type = resp.headers.get("content-type", "image/jpeg")

    # Gemini 호출 (새 SDK)
    image_part = types.Part.from_bytes(data=image_bytes, mime_type=content_type)
    response = client.models.generate_content(
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
    lines = []
    lines.append("📋 공사현장 위험요소 분석 결과\n")
    lines.append(f"[현장 상황]\n{result.get('scene_summary', '')}\n")

    hazards = result.get("hazards", [])
    if not hazards:
        lines.append("위험요소가 감지되지 않았습니다.")
    else:
        for i, h in enumerate(hazards, 1):
            code = h.get("code", "")
            name = RISK_CODE_KR.get(code, code)
            level = h.get("risk_level", "하")
            emoji = RISK_LEVEL_EMOJI.get(level, "")
            lines.append(f"[위험요소 {i}] {emoji} {name}")
            lines.append(f"위험도: {level}")
            lines.append(f"근거: {h.get('reason', '')}")
            lines.append(f"즉시 조치: {h.get('action', '')}\n")

    uncertainty = result.get("uncertainty_note", "")
    if uncertainty:
        lines.append(f"⚠️ {uncertainty}\n")

    lines.append("※ 본 결과는 사진 기반 1차 분석이며 최종 판단은 현장 점검이 필요합니다.")
    return "\n".join(lines)

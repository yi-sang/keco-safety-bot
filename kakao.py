import json

import kosha


def parse_image_url(action: dict) -> str | None:
    """카카오 이미지 보안전송 플러그인 파라미터에서 첫 번째 이미지 URL 추출"""
    try:
        params = action.get("params", {})
        raw = params.get("이미지") or params.get("secureimage")
        if not raw:
            return None

        data = json.loads(raw)
        secure_urls_str = data.get("secureUrls", "")

        # "List(url1, url2, ...)" 형식 파싱
        if secure_urls_str.startswith("List(") and secure_urls_str.endswith(")"):
            urls = secure_urls_str[5:-1].split(", ")
        else:
            urls = [secure_urls_str]

        return urls[0] if urls else None
    except Exception:
        return None


def make_simple_text(text: str) -> dict:
    """카카오 simpleText 응답 포맷 생성"""
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "simpleText": {
                        "text": text
                    }
                }
            ]
        }
    }


def make_error_response(message: str = "분석 중 오류가 발생했습니다. 다시 시도해주세요.") -> dict:
    return make_simple_text(message)


def make_no_image_response() -> dict:
    return make_simple_text("공사현장 사진을 보내주세요. 위험요소를 분석해드립니다.")


# ── 텍스트 카드 (버튼/링크용) ──────────────────────────────
#
# simpleText 는 1000자까지 되지만 버튼을 붙일 수 없다.
# textCard 는 버튼을 붙일 수 있는 대신 title+description 합쳐 400자가 한계다.
# 따라서 분석 본문은 simpleText 로 두고, 링크가 필요한 부분만 textCard 를
# 말풍선 하나로 더 붙이는 조합이 낫다. (make_outputs 참고)

TEXT_CARD_LIMIT = 400          # title + description 합산
BUTTON_LABEL_LIMIT = 14        # 가로 2개 배치 시에는 8자
MAX_BUTTONS_VERTICAL = 3
MAX_BUTTONS_HORIZONTAL = 2


def make_text_card(title: str, description: str, buttons: list[dict] | None = None,
                   vertical: bool = True) -> dict:
    """textCard 말풍선 하나를 만든다. buttons 는 make_web_link_button 등의 결과."""
    buttons = buttons or []
    limit = MAX_BUTTONS_VERTICAL if vertical else MAX_BUTTONS_HORIZONTAL
    buttons = buttons[:limit]

    # 합산 400자를 넘으면 description 부터 줄인다 (title 은 식별에 필요).
    room = TEXT_CARD_LIMIT - len(title)
    if len(description) > room:
        description = description[:max(room - 1, 0)].rstrip() + "…"

    card = {"title": title, "description": description}
    if buttons:
        card["buttons"] = buttons
        card["buttonLayout"] = "vertical" if vertical else "horizontal"
    return {"textCard": card}


def make_web_link_button(label: str, url: str) -> dict:
    return {"action": "webLink", "label": label[:BUTTON_LABEL_LIMIT], "webLinkUrl": url}


def make_message_button(label: str, message: str) -> dict:
    return {"action": "message", "label": label[:BUTTON_LABEL_LIMIT], "messageText": message}


def make_outputs(*outputs: dict) -> dict:
    """말풍선 여러 개를 하나의 스킬 응답으로 묶는다 (예: simpleText + textCard)."""
    return {"version": "2.0", "template": {"outputs": [o for o in outputs if o]}}


def make_simple_text_output(text: str) -> dict:
    """make_outputs 에 넘길 simpleText 말풍선 (응답 전체가 아니라 말풍선 하나)."""
    return {"simpleText": {"text": text}}


def make_analysis_response(text: str, risk_codes: list[str]) -> dict:
    """사진분석 응답: 본문은 simpleText, KOSHA 자료 링크는 textCard 로 덧붙인다.

    본문이 1000자까지 필요한데 textCard 는 400자가 한계라 한 말풍선에 못 담는다.
    자료를 못 찾으면 textCard 없이 기존과 동일한 simpleText 응답이 나간다.
    """
    for code in risk_codes:
        media = kosha.media_for_code(code)
        if not media:
            continue
        return make_outputs(
            make_simple_text_output(text),
            make_text_card(
                "📎 KOSHA 안전보건자료",
                media["title"],
                [make_web_link_button("자료 원문 보기", media["url"])],
            ),
        )
    return make_simple_text(text)

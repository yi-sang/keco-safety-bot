import json


def parse_image_url(action: dict) -> str | None:
    """카카오 이미지 보안전송 플러그인 파라미터에서 첫 번째 이미지 URL 추출"""
    try:
        params = action.get("params", {})
        raw = params.get("secureimage")
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

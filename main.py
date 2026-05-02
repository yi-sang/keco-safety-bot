import httpx
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from kakao import parse_image_url, make_simple_text, make_error_response, make_no_image_response
from gemini import analyze_image

app = FastAPI()


async def process_and_callback(callback_url: str, image_url: str):
    """백그라운드에서 이미지 분석 후 callbackUrl로 결과 전송"""
    try:
        result_text = await analyze_image(image_url)
        print(f"[CALLBACK RESULT] {result_text[:100]}")
        response_body = make_simple_text(result_text)
    except Exception as e:
        import traceback
        print(f"[CALLBACK ERROR] {e}")
        print(traceback.format_exc())
        response_body = make_error_response()

    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            resp = await http_client.post(callback_url, json=response_body)
            print(f"[CALLBACK POST] status={resp.status_code} body={resp.text}")
    except Exception as e:
        print(f"[CALLBACK POST ERROR] {e}")


@app.post("/skill")
async def skill(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
        print(f"[REQUEST] {body}")
        action = body.get("action", {})
        user_request = body.get("userRequest", {})
        callback_url = user_request.get("callbackUrl")

        print(f"[ACTION PARAMS] {action.get('params', {})}")
        print(f"[CALLBACK URL] {callback_url}")
        image_url = parse_image_url(action)
        print(f"[IMAGE URL] {image_url}")

        if not image_url:
            return JSONResponse(content=make_no_image_response())

        if callback_url:
            # 콜백 방식: 즉시 응답 후 백그라운드에서 Gemini 처리
            background_tasks.add_task(process_and_callback, callback_url, image_url)
            return JSONResponse(content={
                "version": "2.0",
                "useCallback": True,
                "data": {
                    "text": "🔍 사진을 분석하고 있어요. 잠시만 기다려 주세요!"
                }
            })
        else:
            # 콜백 URL 없을 경우 기존 동기 방식 (5초 제한 주의)
            result_text = await analyze_image(image_url)
            print(f"[RESULT] {result_text[:100]}")
            return JSONResponse(content=make_simple_text(result_text))

    except Exception as e:
        import traceback
        print(f"[ERROR] {e}")
        print(traceback.format_exc())
        return JSONResponse(content=make_error_response())


@app.get("/")
def health_check():
    return {"status": "ok"}


@app.get("/skill")
def skill_health():
    return {"status": "ok"}

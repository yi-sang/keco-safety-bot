from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from kakao import parse_image_url, make_simple_text, make_error_response, make_no_image_response
from gemini import analyze_image

app = FastAPI()


@app.post("/skill")
async def skill(request: Request):
    try:
        body = await request.json()
        print(f"[REQUEST] {body}")
        action = body.get("action", {})

        print(f"[ACTION PARAMS] {action.get('params', {})}")
        image_url = parse_image_url(action)
        print(f"[IMAGE URL] {image_url}")

        if not image_url:
            return JSONResponse(content=make_no_image_response())

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

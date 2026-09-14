"""KOSHA 공공데이터 오픈API 응답 스펙 확인용 프로브.

사용법:
    # 1) data.go.kr 상세기능 > 미리보기에서 나온 URL을 통째로 붙여넣기
    python scripts/kosha_probe.py "http://apis.data.go.kr/B552468/xxx/yyy?serviceKey=...&pageNo=1"

    # 2) 등록해둔 별칭으로 호출
    python scripts/kosha_probe.py law --query 추락
    python scripts/kosha_probe.py --list

serviceKey는 URL에 들어있어도 무시하고 항상 .env의 KOSHA_API_KEY(Decoding 키)를 씁니다.
"""

import argparse
import json
import os
import sys
from urllib.parse import parse_qsl, urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("KOSHA_API_KEY")
BASE = "https://apis.data.go.kr/B552468"

# 활용신청 후 data.go.kr 상세기능 탭에서 확인한 값으로 채울 것.
# path 는 BASE 뒤에 붙는 "<서비스명>/<오퍼레이션명>".
ENDPOINTS = {
    "disaster_daily": {
        "label": "건설업 일별 중대재해 현황 (15133935)",
        "path": None,  # TODO: 상세기능에서 확인
        "params": {"searchYearMonthDay": "20210401"},
    },
    "law": {
        "label": "안전보건법령 스마트검색 (15123696) ✔ 확인됨",
        "path": "srch/smartSearch",
        "params": {"searchValue": "추락", "category": "0"},
    },
    "case_board": {
        "label": "국내재해사례 게시판 조회 (15121001)",
        "path": None,  # TODO
        "params": {"business": "건설", "keyword": "추락", "callApiId": ""},
    },
    "death_board": {
        "label": "사고사망 게시판 조회 (15119137)",
        "path": None,  # TODO
        "params": {},
    },
    # 검색으로 확인된 유일한 실제 엔드포인트 (국내재해사례 첨부파일)
    "case_attach": {
        "label": "국내재해사례 첨부파일 조회",
        "path": "disaster_attach_api02/Disaster_attach_api02",
        "params": {"boardno": "1", "callApiId": ""},
    },
}

# 포털 API마다 JSON 요청 파라미터 이름이 제각각이라 순서대로 시도한다.
JSON_FLAGS = [{"_type": "json"}, {"type": "json"}, {"dataType": "JSON"}, {}]

COMMON = {"pageNo": "1", "numOfRows": "3"}


def call(url: str, params: dict) -> None:
    for flag in JSON_FLAGS:
        p = {**COMMON, **params, **flag, "serviceKey": API_KEY}
        p = {k: v for k, v in p.items() if v != ""}
        try:
            r = httpx.get(url, params=p, timeout=15.0)
        except Exception as e:
            print(f"  ✗ 요청 실패 ({flag or 'no-flag'}): {type(e).__name__}: {e}")
            continue

        body = r.text.strip()
        tag = flag and next(iter(flag)) or "(플래그 없음)"
        print(f"  · {tag:<10} HTTP {r.status_code}  {r.headers.get('content-type', '?')}  {len(body)}B")

        # 포털 에러는 형식 무관하게 XML로 내려온다
        if "SERVICE_KEY_IS_NOT_REGISTERED" in body:
            print("    → 에러 30 '등록되지 않은 서비스키'. 원인 셋 중 하나:")
            print("      (a) 승인 직후 반영 대기중 (수 분~1시간)")
            print("      (b) 이 API를 활용신청하지 않음 (키는 API별로 등록됨)")
            print("      (c) 엔드포인트 경로가 틀림 - 게이트웨이가 같은 에러로 응답함")
            return
        if "SERVICE ERROR" in body or "<returnAuthMsg>" in body:
            print(f"    → 포털 에러 응답:\n{_indent(body[:600])}")
            return

        if body.startswith("{") or body.startswith("["):
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                print(_indent(body[:1500]))
                return
            print("    → JSON 응답 확보. 구조:")
            print(_indent(json.dumps(data, ensure_ascii=False, indent=2)[:3000]))
            return

        # XML이면 일단 보여주고 다음 플래그 시도
        print(_indent(body[:800]))

    print("  ✗ 모든 JSON 플래그 실패. 위 XML 응답으로 필드명을 확인할 것.")


def _indent(s: str) -> str:
    return "\n".join("      " + line for line in s.splitlines())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", help="별칭 또는 전체 URL")
    ap.add_argument("--list", action="store_true", help="등록된 별칭 출력")
    ap.add_argument("--query", help="검색어 (searchValue/keyword 를 덮어씀)")
    args = ap.parse_args()

    if args.list or not args.target:
        print("등록된 별칭:")
        for k, v in ENDPOINTS.items():
            state = v["path"] or "※ path 미설정"
            print(f"  {k:<16} {v['label']}\n  {'':<16} {state}")
        return

    if not API_KEY:
        sys.exit("KOSHA_API_KEY 가 없습니다. .env 에 Decoding 키를 넣으세요.")

    if args.target.startswith("http"):
        u = urlparse(args.target)
        url = f"{u.scheme}://{u.netloc}{u.path}"
        params = {k: v for k, v in parse_qsl(u.query) if k.lower() != "servicekey"}
        print(f"\n▶ {url}")
        call(url, params)
        return

    cfg = ENDPOINTS.get(args.target)
    if not cfg:
        sys.exit(f"알 수 없는 별칭: {args.target} (--list 로 확인)")
    if not cfg["path"]:
        sys.exit(f"{args.target}: path 가 아직 비어있습니다. data.go.kr 상세기능에서 확인 후 채우세요.")

    params = dict(cfg["params"])
    if args.query:
        for k in ("searchValue", "keyword"):
            if k in params:
                params[k] = args.query

    print(f"\n▶ {cfg['label']}")
    call(f"{BASE}/{cfg['path']}", params)


if __name__ == "__main__":
    main()

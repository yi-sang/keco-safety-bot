"""위험코드별 법령 조문과 안전보건 자료를 KOSHA API에서 조회해 캐시로 저장.

위험코드는 5개로 고정이고 해당 조문도 바뀌지 않으므로, 런타임마다 조회하는 대신
빌드 시점에 한 번 받아 data/kosha_cache.json 에 캐시한다.
법령 개정이나 자료 교체가 필요할 때만 다시 돌리면 된다.

    python scripts/build_kosha_cache.py
"""

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kosha  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "kosha_cache.json"
LIMIT = 2
CASES_PER_CODE = 2


def pick_media(rows: list[dict], code: str) -> dict | None:
    """건설업 자료 중 해당 위험코드 1건. 외국어판·행정공지 제외, 실무자료 우선."""
    hits = [
        m for m in rows
        if kosha.classify(m["MED_SJ_NM"], kosha.MEDIA_RULES) == code
        and not kosha.FOREIGN_RE.search(m["MED_SJ_NM"])
        and not kosha.ADMIN_RE.search(m["MED_SJ_NM"])
    ]
    # 실무 자료 우선, 그 안에서 최신순.
    hits.sort(
        key=lambda m: (bool(kosha.MEDIA_PREFER_RE.search(m["MED_SJ_NM"])),
                       m.get("MED_COMPY_DY", "")),
        reverse=True,
    )
    return hits[0] if hits else None


def pick_cases(rows: list[dict], code: str, n: int) -> list[dict]:
    """해당 위험코드로 분류되는 최신 사례 n건."""
    hits = [
        c for c in rows
        if kosha.classify(c.get("keyword", "") or "", kosha.CASE_RULES) == code
        and kosha.case_date(c)[0] >= kosha.CASE_MIN_YEAR
    ]
    hits.sort(key=kosha.case_date, reverse=True)
    return hits[:n]


async def main() -> None:
    if not kosha.API_KEY:
        sys.exit("KOSHA_API_KEY 가 없습니다. .env 를 확인하세요.")

    print("건설업 자료·재해사례 수집 중…")
    all_media = await kosha.fetch_construction_media()
    all_cases = await kosha.fetch_construction_cases()
    print(f"  자료 {len(all_media)}건, 재해사례 {len(all_cases)}건 (건설업·첨부보유)")

    codes: dict[str, list[dict]] = {}
    media: dict[str, dict] = {}
    cases: dict[str, list[dict]] = {}
    for code in kosha.RISK_CODE_QUERY:
        laws = await kosha.fetch_laws_for_code(code, limit=LIMIT)
        # score 는 질의마다 달라지는 값이라 캐시에 남기지 않는다.
        codes[code] = [
            {k: v for k, v in law.items() if k not in ("score", "excerpt")}
            for law in laws
        ]
        # 제목만으로는 조문 적합성을 판단할 수 없다(제410조 안전난간 = 궤도작업차량).
        # 캐시에 굳기 전에 본문을 눈으로 확인할 수 있도록 함께 출력한다.
        print(f"\n■ {code} ({len(laws)}건)")
        for law in laws:
            print(f"   {law['source']} {law['title']}")
            print(f"      {law['content'][:78]}…")
        if not laws:
            print("   (없음)")

        item = pick_media(all_media, code)
        if item:
            media[code] = {"title": item["MED_SJ_NM"], "url": item["MED_URL"]}
            print(f"   🔗 자료 {item['MED_COMPY_DY'][:7]}  {item['MED_SJ_NM'][:52]}")

        picked = pick_cases(all_cases, code, CASES_PER_CODE)
        entries = []
        for c in picked:
            url = await kosha.fetch_case_attachment(c["boardno"])
            y, m = kosha.case_date(c)
            entries.append({
                "title": c["keyword"],
                "date": f"{y}.{m:02d}",
                "summary": " ".join(c.get("contents", "").split()),
                "url": url,
            })
            print(f"   📌 사례 {y}.{m:02d}  {c['keyword'][:44]}  {'PDF✓' if url else 'PDF✗'}")
        if entries:
            cases[code] = entries

    missing = [c for c, v in codes.items() if not v]
    if missing:
        sys.exit(f"\n조문을 못 찾은 코드가 있어 저장하지 않습니다: {missing}")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "generated_at": date.today().isoformat(),
                "source": "KOSHA 안전보건법령 스마트검색 (공공데이터포털 15123696)",
                "codes": codes,
                "media": media,
                "cases": cases,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n저장 완료: {OUT.relative_to(OUT.parent.parent)}")


if __name__ == "__main__":
    asyncio.run(main())

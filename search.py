#!/usr/bin/env python3
"""사이트에서 부른 열차 조회.

결과를 data/trains.json 에 쓰고, 역 목록도 data/stations.json 에 갱신한다.
사이트는 request_id 가 자기가 보낸 값과 같아질 때까지 파일을 다시 읽는다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pykorail import Korail, NoResultsError, PykorailError, TrainType
from pykorail.device import profile_by_id, random_profile

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TRAINS_PATH = DATA / "trains.json"
STATIONS_PATH = DATA / "stations.json"

KST = timezone(timedelta(hours=9))

MAX_PAGES = 14  # 하루치를 훑기 위한 조회 반복 상한
PAGE_GAP = 1.0  # 조회 사이 간격(초)

KORAIL_ID = os.environ.get("KORAIL_ID", "").strip()
KORAIL_PW = os.environ.get("KORAIL_PW", "").strip()
DEVICE_ID = os.environ.get("DEVICE_PROFILE_ID", "").strip()

DEP = os.environ.get("SEARCH_DEP", "").strip()
ARR = os.environ.get("SEARCH_ARR", "").strip()
DATE = os.environ.get("SEARCH_DATE", "").strip()
REQUEST_ID = os.environ.get("SEARCH_REQUEST_ID", "").strip()


def log(message: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] {message}", flush=True)


def write_result(payload: dict) -> None:
    DATA.mkdir(exist_ok=True)
    payload["request_id"] = REQUEST_ID
    payload["생성시각"] = datetime.now(KST).isoformat(timespec="seconds")
    TRAINS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    commit()


def commit() -> None:
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    run = lambda *a: subprocess.run(a, cwd=ROOT, capture_output=True, text=True)  # noqa: E731
    run("git", "config", "user.name", "ktx-search-bot")
    run("git", "config", "user.email", "ktx-search-bot@users.noreply.github.com")
    run("git", "add", "data")
    if run("git", "commit", "-m", f"조회 결과 {REQUEST_ID}").returncode != 0:
        return
    for attempt in range(4):
        run("git", "pull", "--rebase", "--quiet")
        if run("git", "push", "--quiet").returncode == 0:
            log("결과를 저장소에 커밋했습니다.")
            return
        time.sleep(2 + attempt * 3)
    log("푸시에 실패했습니다.")


def save_stations(korail: Korail) -> None:
    """역 목록을 갱신한다. 실패해도 조회 자체는 계속한다."""
    try:
        names = sorted({station.name for station in korail.stations.all()})
    except Exception as exc:  # noqa: BLE001
        log(f"역 목록을 받지 못했습니다(무시): {exc}")
        return
    DATA.mkdir(exist_ok=True)
    STATIONS_PATH.write_text(
        json.dumps(names, ensure_ascii=False, indent=0) + "\n", encoding="utf-8"
    )
    log(f"역 {len(names)}개를 저장했습니다.")


def collect_day(korail: Korail, day: datetime) -> list[dict]:
    """하루치 열차를 모은다. 한 번 조회로는 앞쪽 몇 편만 오므로 이어서 훑는다."""
    cursor = day.replace(hour=0, minute=0)
    seen: dict[str, dict] = {}

    for page in range(MAX_PAGES):
        try:
            trains = korail.trains.search(
                DEP,
                ARR,
                depart_after=cursor,
                train_type=TrainType.ALL,
                include_no_seats=True,
                include_waiting_list=True,
            )
        except NoResultsError:
            # 그 시각 이후로 열차가 없다는 뜻. 지금까지 모은 것이 하루치 전부다.
            log(f"{page + 1}차 조회 — 더 이상 열차가 없습니다.")
            break
        except Exception as exc:  # noqa: BLE001
            if seen:
                log(f"{page + 1}차 조회에서 오류({type(exc).__name__}). 여기까지로 마칩니다.")
                break
            raise
        fresh = [t for t in trains if t.dep_date == day.strftime("%Y%m%d")]
        if not fresh:
            break

        added = 0
        last = None
        for train in fresh:
            key = f"{train.train_no}-{train.dep_time}"
            last = train
            if key in seen:
                continue
            added += 1
            seen[key] = {
                "열차번호": train.train_no,
                "종류": train.train_type_name,
                "출발": f"{train.dep_time[:2]}:{train.dep_time[2:4]}",
                "도착": f"{train.arr_time[:2]}:{train.arr_time[2:4]}",
                "소요": train.duration_text,
                "일반실": train.has_general_seat(),
                "특실": train.has_special_seat(),
                "좌석": train.has_seat(),
                "예약대기": train.has_waiting_list(),
            }

        log(f"{page + 1}차 조회 — {len(fresh)}편 중 새로 {added}편 (누적 {len(seen)}편)")
        if added == 0 or last is None:
            break

        # 마지막 열차 다음 분부터 이어서 훑는다.
        cursor = datetime.strptime(
            f"{last.dep_date}{last.dep_time[:4]}", "%Y%m%d%H%M"
        ) + timedelta(minutes=1)
        if cursor.date() != day.date():
            break
        time.sleep(PAGE_GAP)

    return sorted(seen.values(), key=lambda t: t["출발"])


def login(profile, attempts: int = 4):
    """코레일 로그인. 일시적 장애가 잦아 몇 번 다시 시도한다."""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return Korail.logged_in(KORAIL_ID, KORAIL_PW, device_profile=profile)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == attempts:
                break
            wait = 5 * attempt
            log(f"로그인 실패({attempt}/{attempts}) — {type(exc).__name__}. {wait}초 뒤 다시 시도합니다.")
            time.sleep(wait)
    raise last


def main() -> None:
    base = {"출발역": DEP, "도착역": ARR, "날짜": DATE, "열차": [], "오류": None}

    if not (DEP and ARR and DATE):
        base["오류"] = "출발역, 도착역, 날짜가 모두 필요합니다."
        write_result(base)
        sys.exit(1)

    try:
        day = datetime.strptime(DATE, "%Y-%m-%d")
    except ValueError:
        base["오류"] = f"날짜 형식이 잘못됐습니다: {DATE}"
        write_result(base)
        sys.exit(1)

    profile = profile_by_id(DEVICE_ID) or random_profile()

    try:
        korail = login(profile)
    except Exception as exc:  # noqa: BLE001 - timeout 등 라이브러리 밖 예외도 포함
        base["오류"] = ("코레일이 응답하지 않습니다. 잠시 뒤 다시 조회해보세요."
                       if "Timeout" in type(exc).__name__
                       else f"코레일 로그인에 실패했습니다. {exc}")
        write_result(base)
        sys.exit(1)

    log(f"로그인 성공 — {korail.name}")

    with korail:
        save_stations(korail)
        try:
            base["열차"] = collect_day(korail, day)
        except Exception as exc:  # noqa: BLE001
            base["오류"] = f"조회 중 오류가 났습니다. {exc}"
            write_result(base)
            sys.exit(1)

    if not base["열차"]:
        base["오류"] = "해당 날짜에 운행하는 열차를 찾지 못했습니다. 역 이름과 날짜를 확인하세요."

    log(f"총 {len(base['열차'])}편")
    write_result(base)


if __name__ == "__main__":
    main()

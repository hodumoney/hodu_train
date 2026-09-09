#!/usr/bin/env python3
"""KTX 취소표 감시 봇.

목표 열차마다 두 단계로 움직인다.

  1. 좌석 감시  — 25초마다 좌석이 났는지 본다.
                  · 좌석이 나면 예약을 잡고 알린다.
                  · 매진인데 코레일 예약대기가 열려 있으면 대기를 신청하고 2단계로 넘어간다.
  2. 대기 확인  — 좌석 감시를 멈추고, 신청한 예약대기가 배정됐는지만 본다.
                  · 배정되면 알린다.
                  · 대기가 사라지면 알리고 그 목표를 끝낸다.

결제는 하지 않는다(설정으로 켤 수는 있다). 알림은 텔레그램으로 간다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from pykorail import (
    AdultPassenger,
    ChildPassenger,
    Korail,
    KorailError,
    NeedToLoginError,
    NoResultsError,
    PastDepartureError,
    PykorailError,
    ReserveOption,
    SeniorPassenger,
    StationNotFoundError,
    ToddlerPassenger,
    TrainType,
)
from pykorail.device import profile_by_id, random_profile

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"

CYCLE_SECONDS = 25  # 한 바퀴가 최소 이만큼은 걸리게 한다
GAP_SECONDS = 3  # 목표와 목표 사이 간격
RELOAD_EVERY = 5  # 몇 바퀴마다 저장소에서 설정을 다시 받아올지
WAIT_CHECK_EVERY = 5  # 몇 바퀴마다 예약대기 배정 여부를 확인할지
ALERT_REPEAT = 3  # 사람이 손을 써야 하는 알림의 반복 횟수
ALERT_GAP_SECONDS = 60

KST = timezone(timedelta(hours=9))

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "25"))
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}

KORAIL_ID = os.environ.get("KORAIL_ID", "").strip()
KORAIL_PW = os.environ.get("KORAIL_PW", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DEVICE_ID = os.environ.get("DEVICE_PROFILE_ID", "").strip()

CARD_NUMBER = os.environ.get("CARD_NUMBER", "").replace("-", "").replace(" ", "").strip()
CARD_PASSWORD = os.environ.get("CARD_PASSWORD", "").strip()
CARD_VERIFY = os.environ.get("CARD_VERIFY", "").strip()
CARD_EXPIRE = os.environ.get("CARD_EXPIRE", "").replace("/", "").strip()
CARD_CORPORATE = os.environ.get("CARD_CORPORATE", "").strip().lower() in {"1", "true", "yes"}


# ─────────────────────────── 기본 도구 ───────────────────────────


def log(message: str) -> None:
    print(f"[{datetime.now(KST):%m-%d %H:%M:%S}] {message}", flush=True)


def notify(text: str) -> None:
    """텔레그램으로 보낸다. 실패해도 감시는 계속한다."""
    if not TG_TOKEN or not TG_CHAT:
        log(f"(텔레그램 미설정) {text}")
        return
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text},
            timeout=15,
        )
        if res.status_code != 200:
            log(f"텔레그램 전송 실패 {res.status_code}: {res.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        log(f"텔레그램 전송 실패: {exc}")


def notify_repeatedly(text: str) -> None:
    """사람이 손을 써야 하는 알림. 놓치지 않도록 1분 간격으로 여러 번 보낸다."""
    notify(text)
    for _ in range(ALERT_REPEAT - 1):
        time.sleep(ALERT_GAP_SECONDS)
        notify(text)


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log(f"{path.name} 을 읽을 수 없습니다 (JSON 형식 오류): {exc}")
        sys.exit(1)


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        run = lambda *a: subprocess.run(a, cwd=ROOT, capture_output=True, text=True)  # noqa: E731
        run("git", "config", "user.name", "ktx-watch-bot")
        run("git", "config", "user.email", "ktx-watch-bot@users.noreply.github.com")
        run("git", "add", "state.json")
        if run("git", "commit", "-m", "예약 기록 갱신").returncode != 0:
            return
        for attempt in range(3):
            run("git", "pull", "--rebase", "--quiet")
            if run("git", "push", "--quiet").returncode == 0:
                log("예약 기록을 저장소에 커밋했습니다.")
                return
            time.sleep(2 + attempt * 3)
        log("커밋 푸시에 실패했습니다. 로그를 확인하세요.")
    except Exception as exc:  # noqa: BLE001
        log(f"커밋 중 오류(무시하고 계속): {exc}")


def pull_repo() -> bool:
    """저장소에서 최신 설정을 받아온다. 사이트에서 목표를 바꿨을 때 반영하기 위함."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return False
    try:
        before = CONFIG_PATH.read_bytes()
        subprocess.run(
            ["git", "pull", "--rebase", "--quiet"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        return CONFIG_PATH.read_bytes() != before
    except Exception as exc:  # noqa: BLE001
        log(f"설정 갱신 실패(무시): {exc}")
        return False


def login(profile, attempts: int = 4):
    """코레일 로그인. 일시적 장애가 잦아 몇 번 다시 시도한다."""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return Korail.logged_in(KORAIL_ID, KORAIL_PW, device_profile=profile)
        except Exception as exc:  # noqa: BLE001 - timeout 등 라이브러리 밖 예외도 포함
            last = exc
            if attempt == attempts:
                break
            wait = 10 * attempt
            log(f"로그인 실패({attempt}/{attempts}) — {type(exc).__name__}. {wait}초 뒤 다시 시도합니다.")
            time.sleep(wait)
    raise last


# ─────────────────────────── 결제(기본은 꺼져 있음) ───────────────────────────


def build_card():
    from pykorail import Card

    missing = [
        name for name, value in (
            ("CARD_NUMBER", CARD_NUMBER), ("CARD_PASSWORD", CARD_PASSWORD),
            ("CARD_VERIFY", CARD_VERIFY), ("CARD_EXPIRE", CARD_EXPIRE),
        ) if not value
    ]
    if missing:
        return None, f"{', '.join(missing)} 가 설정되지 않음"

    problems = []
    if not CARD_NUMBER.isdigit() or not 15 <= len(CARD_NUMBER) <= 16:
        problems.append("CARD_NUMBER 는 하이픈 없는 15~16자리 숫자여야 합니다")
    if not CARD_PASSWORD.isdigit() or len(CARD_PASSWORD) != 2:
        problems.append("CARD_PASSWORD 는 카드 비밀번호 앞 2자리여야 합니다")
    if not CARD_EXPIRE.isdigit() or len(CARD_EXPIRE) != 4:
        problems.append("CARD_EXPIRE 는 YYMM 4자리여야 합니다 (예: 2812)")
    need = 10 if CARD_CORPORATE else 6
    if not CARD_VERIFY.isdigit() or len(CARD_VERIFY) != need:
        problems.append(
            f"CARD_VERIFY 는 {'사업자등록번호 10자리' if CARD_CORPORATE else '생년월일 YYMMDD 6자리'}여야 합니다")
    if problems:
        return None, " / ".join(problems)

    return Card(number=CARD_NUMBER, password=CARD_PASSWORD, verify_number=CARD_VERIFY,
                expire=CARD_EXPIRE, is_corporate=CARD_CORPORATE), None


def parse_payment(config: dict) -> dict:
    raw = config.get("자동결제") or {}
    return {
        "사용": bool(raw.get("사용", False)),
        "1인당상한": int(raw.get("1인당상한", 0)),
        "1건당상한": int(raw.get("1건당상한", 0)),
        "최대건수": int(raw.get("최대건수", 0)),
    }


def price_cap(pay_cfg: dict, people: int) -> int:
    """이 목표에 허용되는 결제 상한. 0 이면 상한 없음."""
    if pay_cfg["1인당상한"]:
        return pay_cfg["1인당상한"] * max(1, people)
    return pay_cfg["1건당상한"]


# ─────────────────────────── 설정 해석 ───────────────────────────


def target_id(target: dict) -> str:
    return f"{target['출발역']}>{target['도착역']} {target['날짜']} {target['시각']}"


def parse_targets(config: dict) -> list[dict]:
    raw = config.get("감시목표")
    if not isinstance(raw, list) or not raw:
        log("config.json 에 '감시목표' 목록이 없습니다.")
        sys.exit(1)

    targets = []
    for index, item in enumerate(raw, start=1):
        if not item.get("사용", True):
            continue
        try:
            depart = datetime.strptime(f"{item['날짜']} {item['시각']}", "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            log(f"{index}번 목표의 날짜/시각 형식이 잘못됐습니다. "
                f"날짜는 2026-09-20, 시각은 18:30 형태여야 합니다.")
            sys.exit(1)

        counts = {
            "어른": int(item.get("어른", 1)), "어린이": int(item.get("어린이", 0)),
            "경로": int(item.get("경로", 0)), "유아": int(item.get("유아", 0)),
        }
        people = sum(counts.values())
        if people < 1:
            log(f"{index}번 목표의 인원이 0명입니다.")
            sys.exit(1)

        passengers = []
        for kind, cls in (("어른", AdultPassenger), ("어린이", ChildPassenger),
                          ("경로", SeniorPassenger), ("유아", ToddlerPassenger)):
            if counts[kind]:
                passengers.append(cls(counts[kind]))

        targets.append({
            "이름": item.get("이름") or target_id(item),
            "출발역": item["출발역"], "도착역": item["도착역"],
            "날짜": item["날짜"], "시각": item["시각"],
            "열차번호": str(item["열차번호"]).strip() if item.get("열차번호") else None,
            "예약대기": bool(item.get("예약대기", True)),
            "id": target_id(item),
            "depart": depart,
            "dep_date": depart.strftime("%Y%m%d"),
            "dep_hhmm": depart.strftime("%H%M"),
            "passengers": passengers,
            "인원": people,
        })
    return targets


def pick_train(trains, target: dict):
    """조회 결과에서 목표 시각과 정확히 일치하는 열차를 고른다."""
    matches = [t for t in trains
               if t.dep_date == target["dep_date"] and t.dep_time[:4] == target["dep_hhmm"]]
    if target["열차번호"]:
        matches = [t for t in matches if t.train_no == target["열차번호"]]
    if len(matches) > 1:
        ktx = [t for t in matches if t.train_type == TrainType.KTX]
        if ktx:
            matches = ktx
    return matches[0] if matches else None


def buy_deadline(reservation) -> str:
    d, t = reservation.buy_limit_date, reservation.buy_limit_time
    return f"{d[4:6]}/{d[6:8]} {t[:2]}:{t[2:4]}"


# ─────────────────────────── 2단계: 예약대기 확인 ───────────────────────────


def check_waiting(target: dict, state: dict, reservations) -> None:
    """신청해둔 예약대기가 배정됐는지 본다. 좌석 조회는 하지 않는다."""
    tid = target["id"]
    name = target["이름"]
    record = state["예약대기"][tid]
    rsv_id = record.get("예약번호")

    found = next((r for r in reservations if r.rsv_id == rsv_id), None)

    if found is None:
        # 마감됐거나, 앱에서 취소했거나, 결제 기한을 넘겼다. 어느 쪽이든 대기는 없다.
        log(f"[{name}] 예약대기가 목록에서 사라졌습니다. 이 목표를 끝냅니다.")
        state["예약완료"][tid] = {
            "결과": "예약대기 사라짐", "시각": datetime.now(KST).isoformat()}
        del state["예약대기"][tid]
        save_state(state)
        notify(
            f"⚠️ 예약대기가 사라졌습니다\n\n{name}\n{target['출발역']} → {target['도착역']} "
            f"{target['날짜']} {target['시각']}\n\n"
            f"대기가 마감됐거나 취소된 것으로 보입니다. 이 열차는 더 이상 보지 않습니다.\n"
            f"다시 노리려면 사이트에서 대기를 새로 걸어주세요."
        )
        return

    if found.is_waiting:
        log(f"[{name}] 예약대기 유지 중 — 아직 배정되지 않았습니다.")
        return

    # 구입기한이 채워졌다 = 자리가 배정됐다.
    deadline = buy_deadline(found)
    log(f"[{name}] 예약대기 배정 — {found.price:,}원, 기한 {deadline}")
    state["예약완료"][tid] = {
        "예약번호": found.rsv_id, "금액": found.price, "결제기한": deadline,
        "결제": "안 됨", "사유": "예약대기 배정 — 직접 결제",
        "시각": datetime.now(KST).isoformat(),
    }
    del state["예약대기"][tid]
    save_state(state)
    notify_repeatedly(
        f"🎉 예약대기가 배정됐습니다\n\n{name}\n{found.train.summary()}\n"
        f"{found.seat_no_count}석 · {found.price:,}원\n\n"
        f"⏰ 결제 기한: {deadline}\n"
        f"코레일톡 앱에서 결제하세요. 기한이 지나면 취소됩니다."
    )


# ─────────────────────────── 1단계: 좌석 감시 ───────────────────────────


def handle_target(korail, target: dict, state: dict, pay_cfg: dict, card, card_error) -> None:
    tid = target["id"]
    name = target["이름"]

    trains = korail.trains.search(
        target["출발역"], target["도착역"],
        depart_after=target["depart"], train_type=TrainType.ALL,
        passengers=target["passengers"],
        include_no_seats=True, include_waiting_list=True,
    )
    train = pick_train(trains, target)

    if train is None:
        if tid not in state["없는열차"]:
            state["없는열차"].append(tid)
            save_state(state)
            notify(f"⚠️ 열차를 찾지 못했습니다\n\n{name}\n{tid}\n\n"
                   f"config.json 의 역 이름·날짜·시각을 확인해주세요.")
        return
    if tid in state["없는열차"]:
        state["없는열차"].remove(tid)
        save_state(state)

    # ── 좌석이 났다 ──
    if train.has_seat():
        grade = "일반실" if train.has_general_seat() else "특실"
        log(f"[{name}] 좌석 발견 — {train.summary()} ({grade})")

        if DRY_RUN:
            notify(f"🧪 [테스트] 좌석을 찾았습니다 (예약하지 않음)\n\n{name}\n{train.summary()}\n{grade}")
            state["예약완료"][tid] = {"테스트": True, "시각": datetime.now(KST).isoformat()}
            save_state(state)
            return

        try:
            reservation = korail.reservations.create(
                train, passengers=target["passengers"], option=ReserveOption.GENERAL_FIRST)
        except KorailError as exc:
            log(f"[{name}] 예약 실패 — {exc.msg} ({exc.code}). 계속 감시합니다.")
            return

        deadline = buy_deadline(reservation)
        detail = (f"{name}\n{train.summary()}\n"
                  f"{reservation.seat_no_count}석 · {reservation.price:,}원")
        record = {"예약번호": reservation.rsv_id, "금액": reservation.price,
                  "결제기한": deadline, "시각": datetime.now(KST).isoformat()}

        # 자동결제는 기본으로 꺼져 있다. 켜져 있을 때만 아래를 탄다.
        if pay_cfg["사용"]:
            reason = None
            if card is None:
                reason = f"카드 정보 문제 — {card_error}"
            elif reservation.price > (cap := price_cap(pay_cfg, target["인원"])) > 0:
                reason = f"금액이 상한({cap:,}원 · {target['인원']}명)을 넘습니다"
            elif pay_cfg["최대건수"] and state["결제건수"] >= pay_cfg["최대건수"]:
                reason = f"자동결제 건수 상한({pay_cfg['최대건수']}건)에 도달했습니다"
            elif (reservation.train.dep_date != target["dep_date"]
                  or reservation.train.dep_time[:4] != target["dep_hhmm"]):
                reason = "예약된 열차가 목표와 다릅니다"

            if reason is None:
                try:
                    korail.reservations.pay(reservation, card)
                except KorailError as exc:
                    reason = f"결제가 거부됐습니다 — {exc.msg}"
                else:
                    log(f"[{name}] 결제 완료 — {reservation.price:,}원")
                    record |= {"결제": "완료"}
                    state["예약완료"][tid] = record
                    state["결제건수"] = state.get("결제건수", 0) + 1
                    save_state(state)
                    notify(f"✅ 예약·결제 완료\n\n{detail}\n\n코레일톡에서 승차권을 확인하세요.")
                    return
            record |= {"결제": "안 됨", "사유": reason}
            extra = f"사유: {reason}\n\n"
        else:
            record |= {"결제": "안 됨", "사유": "직접 결제"}
            extra = ""

        state["예약완료"][tid] = record
        save_state(state)
        notify_repeatedly(
            f"🎫 좌석을 잡았습니다\n\n{detail}\n\n"
            f"⏰ 결제 기한: {deadline}\n{extra}"
            f"코레일톡 앱에서 결제하세요. 기한이 지나면 자동 취소됩니다."
        )
        return

    # ── 매진. 예약대기가 열려 있으면 신청하고 2단계로 넘긴다 ──
    if target["예약대기"] and train.has_waiting_list():
        log(f"[{name}] 매진 — 예약대기를 신청합니다.")
        if DRY_RUN:
            notify(f"🧪 [테스트] 예약대기 가능 (신청하지 않음)\n\n{name}\n{train.summary()}")
            state["예약완료"][tid] = {"테스트": True, "시각": datetime.now(KST).isoformat()}
            save_state(state)
            return
        try:
            reservation = korail.reservations.create(
                train, passengers=target["passengers"], option=ReserveOption.GENERAL_FIRST)
        except KorailError as exc:
            log(f"[{name}] 예약대기 신청 실패 — {exc.msg} ({exc.code})")
            return

        state["예약대기"][tid] = {
            "예약번호": reservation.rsv_id, "시각": datetime.now(KST).isoformat()}
        save_state(state)
        notify(
            f"⏳ 코레일 예약대기를 신청했습니다\n\n{name}\n{train.summary()}\n\n"
            f"이제 취소표 감시는 멈추고, 자리가 배정되는지만 확인합니다.\n"
            f"배정되면 바로 알려드릴게요."
        )
        return

    log(f"[{name}] 매진 — {train.summary()}")


# ─────────────────────────── 메인 ───────────────────────────


def main() -> None:
    if not KORAIL_ID or not KORAIL_PW:
        log("KORAIL_ID / KORAIL_PW 가 설정되지 않았습니다. Secrets 를 확인하세요.")
        sys.exit(1)

    config = load_json(CONFIG_PATH, {"감시목표": []})
    state = load_json(STATE_PATH, {})
    state.setdefault("예약완료", {})
    state.setdefault("없는열차", [])
    state.setdefault("결제건수", 0)
    # 예전 형식(목록)을 예약번호를 담는 형태로 옮긴다.
    waiting = state.get("예약대기", {})
    if isinstance(waiting, list):
        waiting = {tid: {"예약번호": None} for tid in waiting}
    state["예약대기"] = waiting

    pay_cfg = parse_payment(config)
    card, card_error = build_card()
    targets = parse_targets(config)

    log(f"목표 {len(targets)}개" + (" · 테스트 모드" if DRY_RUN else ""))
    if pay_cfg["사용"]:
        cap = (f"1인당 {pay_cfg['1인당상한']:,}원" if pay_cfg["1인당상한"]
               else f"1건당 {pay_cfg['1건당상한']:,}원" if pay_cfg["1건당상한"] else "상한 없음")
        log(f"자동결제: 켜짐 · {cap}" + (f" · 카드 문제: {card_error}" if card is None else ""))
    else:
        log("자동결제: 꺼짐 — 예약만 잡고 알립니다.")

    profile = profile_by_id(DEVICE_ID) or random_profile()
    try:
        korail = login(profile)
    except Exception as exc:  # noqa: BLE001
        log(f"코레일 로그인 실패: {type(exc).__name__} {exc}")
        if "Timeout" not in type(exc).__name__:
            notify(f"❌ 코레일 로그인에 실패했습니다.\n\n{exc}")
        else:
            log("코레일 응답 지연입니다. 알림 없이 다음 실행에 맡깁니다.")
        sys.exit(1)

    log(f"로그인 성공 — {korail.name}")
    deadline = time.time() + RUN_MINUTES * 60
    cycle = 0

    with korail:
        while time.time() < deadline:
            cycle_start = time.time()
            cycle += 1

            if cycle % RELOAD_EVERY == 0 and pull_repo():
                try:
                    config = load_json(CONFIG_PATH, {"감시목표": []})
                    targets = parse_targets(config)
                    pay_cfg = parse_payment(config)
                    log(f"설정이 바뀌어 다시 읽었습니다 — 목표 {len(targets)}개")
                except SystemExit:
                    log("바뀐 설정에 문제가 있어 이전 설정을 유지합니다.")

            live = [t for t in targets if t["id"] not in state["예약완료"]]
            if not live:
                log("모든 목표를 처리했습니다. 종료합니다.")
                return

            seat_targets = [t for t in live if t["id"] not in state["예약대기"]]
            wait_targets = [t for t in live if t["id"] in state["예약대기"]]

            if cycle == 1:
                for t in live:
                    stage = "대기 확인" if t["id"] in state["예약대기"] else "좌석 감시"
                    log(f"  · {t['이름']} — {t['id']} ({t['인원']}명, {stage})")

            # 예약대기는 초 단위로 급하지 않으니 몇 바퀴에 한 번만 확인한다.
            if wait_targets and (cycle == 1 or cycle % WAIT_CHECK_EVERY == 0):
                try:
                    reservations = korail.reservations.all()
                except NoResultsError:
                    reservations = []
                except Exception as exc:  # noqa: BLE001
                    log(f"예약 목록 조회 실패(다음에 다시): {type(exc).__name__} {exc}")
                    reservations = None
                if reservations is not None:
                    for target in wait_targets:
                        try:
                            check_waiting(target, state, reservations)
                        except Exception as exc:  # noqa: BLE001
                            log(f"[{target['이름']}] 대기 확인 오류: {type(exc).__name__} {exc}")

            for index, target in enumerate(seat_targets):
                if index:
                    time.sleep(GAP_SECONDS)
                try:
                    handle_target(korail, target, state, pay_cfg, card, card_error)
                except PastDepartureError:
                    log(f"[{target['이름']}] 열차가 이미 출발했습니다. 목표에서 제외합니다.")
                    state["예약완료"][target["id"]] = {
                        "결과": "출발함", "시각": datetime.now(KST).isoformat()}
                    save_state(state)
                    notify(f"🚉 {target['이름']} 열차가 출발했습니다. 감시를 종료합니다.")
                except StationNotFoundError as exc:
                    log(f"[{target['이름']}] 역 이름 오류: {exc}")
                    notify(f"⚠️ {target['이름']} — 역 이름이 잘못됐습니다.\n{exc}")
                except NeedToLoginError:
                    log("세션이 만료됐습니다. 다시 로그인합니다.")
                    try:
                        korail.login(KORAIL_ID, KORAIL_PW)
                    except Exception as exc:  # noqa: BLE001
                        log(f"재로그인 실패: {exc}. 이번 실행을 종료합니다.")
                        return
                except Exception as exc:  # noqa: BLE001 - 통신 오류로 감시가 죽으면 안 된다
                    log(f"[{target['이름']}] 일시적 오류(계속 진행): {type(exc).__name__} {exc}")

            elapsed = time.time() - cycle_start
            if elapsed < CYCLE_SECONDS:
                time.sleep(CYCLE_SECONDS - elapsed)

    log("이번 실행 시간이 끝났습니다. 다음 실행이 이어받습니다.")


if __name__ == "__main__":
    main()

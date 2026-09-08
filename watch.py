#!/usr/bin/env python3
"""KTX 취소표 감시 봇.

config.json 에 적어둔 열차들을 주기적으로 조회해서,
좌석이 나오면 예약을 선점하고 텔레그램으로 알린다.
결제는 하지 않는다.
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
ALERT_REPEAT = 3  # 예약 성공 시 알림 횟수
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


# ─────────────────────────── 유틸 ───────────────────────────


def log(message: str) -> None:
    stamp = datetime.now(KST).strftime("%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def notify(text: str) -> None:
    """텔레그램으로 메시지를 보낸다. 실패해도 감시는 계속한다."""
    if not TG_TOKEN or not TG_CHAT:
        log(f"(텔레그램 미설정) {text}")
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text},
            timeout=15,
        )
        if response.status_code != 200:
            log(f"텔레그램 전송 실패 {response.status_code}: {response.text[:200]}")
    except Exception as exc:  # noqa: BLE001 - 알림 실패로 감시가 죽으면 안 된다
        log(f"텔레그램 전송 실패: {exc}")


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log(f"{path.name} 을 읽을 수 없습니다 (JSON 형식 오류): {exc}")
        sys.exit(1)


def save_state(state: dict) -> None:
    """state.json 을 저장하고, GitHub Actions 안이면 저장소에 커밋한다."""
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        run = lambda *args: subprocess.run(  # noqa: E731
            args, cwd=ROOT, capture_output=True, text=True
        )
        run("git", "config", "user.name", "ktx-watch-bot")
        run("git", "config", "user.email", "ktx-watch-bot@users.noreply.github.com")
        run("git", "add", "state.json")
        committed = run("git", "commit", "-m", "예약 기록 갱신")
        if committed.returncode != 0:
            return  # 바뀐 게 없음
        for attempt in range(3):
            run("git", "pull", "--rebase", "--quiet")
            pushed = run("git", "push", "--quiet")
            if pushed.returncode == 0:
                log("예약 기록을 저장소에 커밋했습니다.")
                return
            time.sleep(2 + attempt * 3)
        log("커밋 푸시에 실패했습니다. 중복 예약 위험이 있으니 로그를 확인하세요.")
    except Exception as exc:  # noqa: BLE001
        log(f"커밋 중 오류(무시하고 계속): {exc}")


# ─────────────────────────── 설정 해석 ───────────────────────────


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


def build_card():
    """Secrets 에 카드 정보가 다 있으면 Card 를 만든다. 하나라도 없으면 None."""
    from pykorail import Card

    missing = [
        name
        for name, value in (
            ("CARD_NUMBER", CARD_NUMBER),
            ("CARD_PASSWORD", CARD_PASSWORD),
            ("CARD_VERIFY", CARD_VERIFY),
            ("CARD_EXPIRE", CARD_EXPIRE),
        )
        if not value
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
    expected_verify = 10 if CARD_CORPORATE else 6
    if not CARD_VERIFY.isdigit() or len(CARD_VERIFY) != expected_verify:
        problems.append(
            f"CARD_VERIFY 는 {'사업자등록번호 10자리' if CARD_CORPORATE else '생년월일 YYMMDD 6자리'}여야 합니다"
        )
    if problems:
        return None, " / ".join(problems)

    return (
        Card(
            number=CARD_NUMBER,
            password=CARD_PASSWORD,
            verify_number=CARD_VERIFY,
            expire=CARD_EXPIRE,
            is_corporate=CARD_CORPORATE,
        ),
        None,
    )


def parse_payment(config: dict) -> dict:
    raw = config.get("자동결제") or {}
    return {
        "사용": bool(raw.get("사용", False)),
        "1건당상한": int(raw.get("1건당상한", 0)),
        "최대건수": int(raw.get("최대건수", 0)),  # 0 이면 무제한
    }


def target_id(target: dict) -> str:
    return f"{target['출발역']}>{target['도착역']} {target['날짜']} {target['시각']}"


def parse_targets(config: dict) -> list[dict]:
    """config.json 을 검사하고 내부에서 쓸 형태로 바꾼다."""
    raw = config.get("감시목표")
    if not isinstance(raw, list) or not raw:
        log("config.json 에 '감시목표' 목록이 없습니다.")
        sys.exit(1)

    targets = []
    for index, item in enumerate(raw, start=1):
        if not item.get("사용", True):
            continue
        try:
            depart = datetime.strptime(
                f"{item['날짜']} {item['시각']}", "%Y-%m-%d %H:%M"
            )
        except (KeyError, ValueError):
            log(f"{index}번 목표의 날짜/시각 형식이 잘못됐습니다. "
                f"날짜는 2026-09-20, 시각은 18:30 형태여야 합니다.")
            sys.exit(1)

        adults = int(item.get("어른", 1))
        children = int(item.get("어린이", 0))
        seniors = int(item.get("경로", 0))
        toddlers = int(item.get("유아", 0))
        if adults + children + seniors + toddlers < 1:
            log(f"{index}번 목표의 인원이 0명입니다.")
            sys.exit(1)

        passengers = []
        if adults:
            passengers.append(AdultPassenger(adults))
        if children:
            passengers.append(ChildPassenger(children))
        if seniors:
            passengers.append(SeniorPassenger(seniors))
        if toddlers:
            passengers.append(ToddlerPassenger(toddlers))

        targets.append(
            {
                "이름": item.get("이름") or target_id(item),
                "출발역": item["출발역"],
                "도착역": item["도착역"],
                "날짜": item["날짜"],
                "시각": item["시각"],
                "열차번호": str(item["열차번호"]).strip() if item.get("열차번호") else None,
                "예약대기": bool(item.get("예약대기", True)),
                "id": target_id(item),
                "depart": depart,
                "dep_date": depart.strftime("%Y%m%d"),
                "dep_hhmm": depart.strftime("%H%M"),
                "passengers": passengers,
                "인원": adults + children + seniors + toddlers,
            }
        )

    return targets


def pick_train(trains, target: dict):
    """조회 결과에서 목표 시각과 정확히 일치하는 열차를 고른다."""
    matches = [
        train
        for train in trains
        if train.dep_date == target["dep_date"]
        and train.dep_time[:4] == target["dep_hhmm"]
    ]
    if target["열차번호"]:
        matches = [t for t in matches if t.train_no == target["열차번호"]]
    if len(matches) > 1:
        ktx = [t for t in matches if t.train_type == TrainType.KTX]
        if ktx:
            matches = ktx
    return matches[0] if matches else None


# ─────────────────────────── 목표 하나 처리 ───────────────────────────


def handle_target(
    korail: Korail, target: dict, state: dict, pay_cfg: dict, card, card_error
) -> None:
    tid = target["id"]
    name = target["이름"]

    trains = korail.trains.search(
        target["출발역"],
        target["도착역"],
        depart_after=target["depart"],
        train_type=TrainType.ALL,
        passengers=target["passengers"],
        include_no_seats=True,
        include_waiting_list=True,
    )
    train = pick_train(trains, target)

    if train is None:
        if tid not in state["없는열차"]:
            state["없는열차"].append(tid)
            save_state(state)
            notify(
                f"⚠️ 열차를 찾지 못했습니다\n\n{name}\n{tid}\n\n"
                f"config.json 의 역 이름·날짜·시각을 확인해주세요. "
                f"이 목표는 계속 조회하지만, 설정이 틀렸을 수 있습니다."
            )
        return

    if tid in state["없는열차"]:
        state["없는열차"].remove(tid)
        save_state(state)

    # ── 좌석이 나왔다 ──
    if train.has_seat():
        grade = "일반실" if train.has_general_seat() else "특실"
        log(f"[{name}] 좌석 발견 — {train.summary()} ({grade})")

        if DRY_RUN:
            notify(
                f"🧪 [테스트] 좌석을 찾았습니다 (예약·결제 모두 하지 않음)\n\n"
                f"{name}\n{train.summary()}\n{grade}"
            )
            state["예약완료"][tid] = {"테스트": True, "시각": datetime.now(KST).isoformat()}
            save_state(state)
            return

        try:
            reservation = korail.reservations.create(
                train, passengers=target["passengers"], option=ReserveOption.GENERAL_FIRST
            )
        except KorailError as exc:
            log(f"[{name}] 예약 실패 — {exc.msg} ({exc.code}). 계속 감시합니다.")
            return

        deadline = f"{reservation.buy_limit_date[4:6]}/{reservation.buy_limit_date[6:8]} " \
                   f"{reservation.buy_limit_time[:2]}:{reservation.buy_limit_time[2:4]}"
        detail = (
            f"{name}\n{train.summary()}\n"
            f"{reservation.seat_no_count}석 · {reservation.price:,}원"
        )
        record = {
            "예약번호": reservation.rsv_id,
            "금액": reservation.price,
            "결제기한": deadline,
            "시각": datetime.now(KST).isoformat(),
        }

        # ── 자동 결제를 할지 판단한다 ──
        skip_reason = None
        if not pay_cfg["사용"]:
            skip_reason = "자동결제가 꺼져 있습니다"
        elif card is None:
            skip_reason = f"카드 정보 문제 — {card_error}"
        elif reservation.price > pay_cfg["1건당상한"] > 0:
            skip_reason = (
                f"금액이 1건당 상한({pay_cfg['1건당상한']:,}원)을 넘습니다"
            )
        elif pay_cfg["최대건수"] and state["결제건수"] >= pay_cfg["최대건수"]:
            skip_reason = f"자동결제 건수 상한({pay_cfg['최대건수']}건)에 도달했습니다"
        elif (
            reservation.train.dep_date != target["dep_date"]
            or reservation.train.dep_time[:4] != target["dep_hhmm"]
        ):
            # 있어선 안 되는 상황이지만, 돈이 나가는 일이라 한 번 더 확인한다.
            skip_reason = "예약된 열차가 목표와 다릅니다"

        if skip_reason is None:
            try:
                korail.reservations.pay(reservation, card)
            except KorailError as exc:
                log(f"[{name}] 결제 거부 — {exc.msg} ({exc.code})")
                skip_reason = f"결제가 거부됐습니다 — {exc.msg}"
            else:
                log(f"[{name}] 결제 완료 — {reservation.price:,}원")
                record["결제"] = "완료"
                state["예약완료"][tid] = record
                state["결제건수"] = state.get("결제건수", 0) + 1
                save_state(state)
                notify(
                    f"✅ 예약 + 결제 완료\n\n{detail}\n\n"
                    f"코레일톡 앱에서 승차권을 확인하세요.\n\n"
                    f"이 열차를 다시 감시하려면 state.json 에서\n\"{tid}\" 줄을 지우세요."
                )
                return

        # ── 결제를 못 했다. 예약은 살아 있으니 급히 알린다 ──
        record["결제"] = "안 됨"
        record["사유"] = skip_reason
        state["예약완료"][tid] = record
        save_state(state)

        message = (
            f"🎫 예약 성공 — 결제는 직접 하세요\n\n{detail}\n\n"
            f"⏰ 결제 기한: {deadline}\n"
            f"사유: {skip_reason}\n\n"
            f"코레일톡 앱에서 결제하세요. 기한이 지나면 자동 취소됩니다.\n\n"
            f"이 열차를 다시 감시하려면 state.json 에서\n\"{tid}\" 줄을 지우세요."
        )
        notify(message)
        for _ in range(ALERT_REPEAT - 1):
            time.sleep(ALERT_GAP_SECONDS)
            notify(message)
        return

    # ── 매진. 예약대기가 열려 있으면 걸어둔다 ──
    if target["예약대기"] and train.has_waiting_list() and tid not in state["예약대기"]:
        log(f"[{name}] 매진 — 예약대기를 신청합니다.")
        if DRY_RUN:
            notify(f"🧪 [테스트] 예약대기 가능 (실제 신청은 하지 않음)\n\n{name}\n{train.summary()}")
            state["예약대기"].append(tid)
            save_state(state)
            return
        try:
            reservation = korail.reservations.create(
                train, passengers=target["passengers"], option=ReserveOption.GENERAL_FIRST
            )
        except KorailError as exc:
            log(f"[{name}] 예약대기 신청 실패 — {exc.msg} ({exc.code})")
            return
        state["예약대기"].append(tid)
        save_state(state)
        notify(
            f"⏳ 예약대기 신청 완료\n\n{name}\n{train.summary()}\n\n"
            f"자리가 나면 코레일이 배정해줍니다. "
            f"좌석 감시는 계속합니다."
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
    state.setdefault("예약대기", [])
    state.setdefault("없는열차", [])
    state.setdefault("결제건수", 0)

    pay_cfg = parse_payment(config)
    card, card_error = build_card()

    targets = parse_targets(config)
    pending = [t for t in targets if t["id"] not in state["예약완료"]]

    log(f"설정된 목표 {len(targets)}개 / 감시 대상 {len(pending)}개"
        + (" · 테스트 모드" if DRY_RUN else ""))
    if not pay_cfg["사용"]:
        log("자동결제: 꺼짐 — 예약만 잡고 알립니다.")
    elif card is None:
        log(f"자동결제: 켜져 있지만 사용할 수 없습니다 — {card_error}")
    else:
        cap = f"{pay_cfg['1건당상한']:,}원" if pay_cfg["1건당상한"] else "상한 없음"
        cnt = f"{pay_cfg['최대건수']}건" if pay_cfg["최대건수"] else "무제한"
        log(f"자동결제: 켜짐 · 1건당 {cap} · 최대 {cnt} · 지금까지 {state['결제건수']}건")
    for t in pending:
        log(f"  · {t['이름']} — {t['id']} ({t['인원']}명)")

    if not pending:
        log("감시할 목표가 없습니다. 종료합니다.")
        return

    profile = profile_by_id(DEVICE_ID) or random_profile()
    if not DEVICE_ID:
        log(f"DEVICE_PROFILE_ID 가 없어 임시 기기로 돕니다: {profile.id}")

    try:
        korail = login(profile)
    except Exception as exc:  # noqa: BLE001
        log(f"코레일 로그인 실패: {type(exc).__name__} {exc}")
        if not isinstance(exc, (TimeoutError,)) and "Timeout" not in type(exc).__name__:
            notify(f"❌ 코레일 로그인에 실패했습니다.\n\n{exc}")
        else:
            log("코레일 응답 지연입니다. 알림은 보내지 않고 다음 실행에 맡깁니다.")
        sys.exit(1)

    log(f"로그인 성공 — {korail.name}")
    deadline = time.time() + RUN_MINUTES * 60

    with korail:
        while time.time() < deadline:
            cycle_start = time.time()
            pending = [t for t in targets if t["id"] not in state["예약완료"]]
            if not pending:
                log("모든 목표를 처리했습니다. 종료합니다.")
                return

            for index, target in enumerate(pending):
                if index:
                    time.sleep(GAP_SECONDS)
                try:
                    handle_target(korail, target, state, pay_cfg, card, card_error)
                except PastDepartureError:
                    log(f"[{target['이름']}] 열차가 이미 출발했습니다. 목표에서 제외합니다.")
                    state["예약완료"][target["id"]] = {
                        "결과": "출발함",
                        "시각": datetime.now(KST).isoformat(),
                    }
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

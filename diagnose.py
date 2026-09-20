#!/usr/bin/env python3
"""코레일 로그인이 왜 거부되는지 보기 위한 일회성 진단 도구.

저장소가 Public 이라 Actions 로그는 누구나 볼 수 있다. 그래서 응답을
날것으로 찍지 않는다. 사유·코드처럼 원인 파악에 필요한 값만 그대로 보이고,
회원번호·이름·이메일 같은 값은 길이만 남긴다.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import version

from pykorail import Korail, LoginFailedError
from pykorail.device import profile_by_id, random_profile

# 원인 파악에 필요하고, 개인정보가 아닌 항목만 값을 보여준다.
SHOW = {
    "strResult", "h_msg_cd", "h_msg_txt", "h_result_cd", "h_result_msg",
    "strAppVer", "strSubAppVer", "h_min_ver", "h_new_ver", "h_upd_yn",
    "strLoginYn", "strCustDvCd", "h_curr_dtm",
    # 코레일 앱 형식이 아닌 오류 봉투로 올 때의 항목들.
    # 개인정보가 아니라 서버가 왜 막았는지를 담은 값이다.
    "code", "message", "error", "error_code", "error_description",
    "status", "statusCode", "detail", "title", "reason", "path",
}

KORAIL_ID = os.environ.get("KORAIL_ID", "").strip()
KORAIL_PW = os.environ.get("KORAIL_PW", "").strip()
DEVICE_ID = os.environ.get("DEVICE_PROFILE_ID", "").strip()


def mask(key: str, value) -> str:
    if key in SHOW:
        return repr(value)
    if isinstance(value, str):
        return f"<문자 {len(value)}자>" if value else "<빈 값>"
    if isinstance(value, (dict, list)):
        return f"<{type(value).__name__} {len(value)}개>"
    return f"<{type(value).__name__}>"


def main() -> None:
    print(f"pykorail {version('pykorail')}")

    if not KORAIL_ID or not KORAIL_PW:
        print("KORAIL_ID / KORAIL_PW 가 비어 있습니다.")
        sys.exit(1)

    # 아이디의 형태만 확인한다. 값 자체는 찍지 않는다.
    kind = ("이메일" if "@" in KORAIL_ID else
            "휴대폰(하이픈 있음)" if "-" in KORAIL_ID else "숫자만")
    print(f"아이디 형태: {kind} · {len(KORAIL_ID)}자 / 비밀번호 {len(KORAIL_PW)}자")
    if KORAIL_ID != KORAIL_ID.strip() or KORAIL_PW != KORAIL_PW.strip():
        print("⚠️ 앞뒤에 공백이 섞여 있습니다. Secret 을 다시 넣어주세요.")

    profile = profile_by_id(DEVICE_ID) or random_profile()
    print(f"기기 프로파일: {profile.id} ({profile.marketing}, Android {profile.android})")

    korail = Korail(device_profile=profile)

    # 라이브러리가 서버와 주고받은 마지막 응답을 붙잡아 둔다.
    seen = []
    original_post = korail._api.post

    def spy(url, *args, **kwargs):
        payload = original_post(url, *args, **kwargs)
        seen.append((url, payload))
        return payload

    korail._api.post = spy

    try:
        korail.login(KORAIL_ID, KORAIL_PW)
    except LoginFailedError as exc:
        print(f"\n거부됨 — {exc} (코드 {exc.code})")
    except Exception as exc:  # noqa: BLE001
        print(f"\n다른 오류 — {type(exc).__name__}: {exc}")
    else:
        print(f"\n로그인 성공. 계정은 정상입니다. (이름 {len(korail.name or '')}자)")
    finally:
        korail.close()

    print(f"\n주고받은 요청 {len(seen)}건")
    for index, (url, payload) in enumerate(seen, start=1):
        tail = url.rsplit("/", 1)[-1]
        print(f"\n[{index}] …/{tail}")
        if not isinstance(payload, dict):
            print(f"    응답이 사전 형태가 아님: {type(payload).__name__}")
            continue
        if not payload:
            print("    응답이 비어 있음 — 서버가 아무 내용도 주지 않았습니다.")
            continue
        for key in sorted(payload):
            print(f"    {key} = {mask(key, payload[key])}")


if __name__ == "__main__":
    main()

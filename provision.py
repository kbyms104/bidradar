#!/usr/bin/env python3
"""고객 계정 관리 도구 (운영자용).

웹 화면 없이 계정을 만들고, 비밀번호를 재설정하고, 초대 코드를 발급합니다.
`data/app.db`를 직접 다룹니다.

예시
    python3 provision.py list
    python3 provision.py add --email boss@acme.co.kr --company "에이스테크" --name "김대표"
    python3 provision.py reset --email boss@acme.co.kr
    python3 provision.py invite --note "파일럿 A사"
    python3 provision.py set --email boss@acme.co.kr --plan starter --status active
"""
from __future__ import annotations

import argparse
import secrets
import sys

import accounts
import config


def _account_row(email: str):
    email = accounts.normalize_email(email)
    for row in accounts.list_accounts():
        if row["email"] == email:
            return row
    return None


def _fmt(ts: int) -> str:
    from datetime import datetime
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return "-"


def cmd_list(args: argparse.Namespace) -> int:
    rows = accounts.list_accounts()
    if not rows:
        print(f"계정 없음 ({config.app_db()})")
        return 0
    print(f"{'ID':>3}  {'이메일':<30} {'회사':<16} {'플랜':<8} {'상태':<7} "
          f"{'웹훅':<5} {'조건':>3} {'발송':>4}  가입")
    for a in rows:
        print(f"{a['id']:>3}  {a['email']:<30} {(a['company'] or '-')[:16]:<16} "
              f"{a['plan']:<8} {a['status']:<7} "
              f"{'O' if a['webhook'] else '-':<5} {a['n_conditions']:>3} {a['n_sent']:>4}  "
              f"{_fmt(a['created_at'])}")
    print(f"\nDB: {config.app_db()}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    password = args.password or secrets.token_urlsafe(9)
    try:
        account_id = accounts.create_account(
            args.email, password, args.company, args.name, args.phone, args.plan)
    except ValueError as exc:
        print(f"실패: {exc}", file=sys.stderr)
        return 1
    print(f"계정 생성 완료: id={account_id} {accounts.normalize_email(args.email)}")
    if not args.password:
        print(f"임시 비밀번호: {password}")
        print("→ 고객에게 안전한 채널로 전달하고, 첫 로그인 후 변경을 안내하세요.")
    print(f"로그인: https://bidradar-kr.duckdns.org/login")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    row = _account_row(args.email)
    if not row:
        print("해당 이메일 계정이 없습니다.", file=sys.stderr)
        return 1
    password = args.password or secrets.token_urlsafe(9)
    try:
        accounts.set_password(row["id"], password)
    except ValueError as exc:
        print(f"실패: {exc}", file=sys.stderr)
        return 1
    accounts.delete_account_sessions(row["id"])
    print(f"비밀번호 재설정 완료: {row['email']}")
    if not args.password:
        print(f"새 비밀번호: {password}")
    print("기존 로그인 세션은 모두 종료했습니다.")
    return 0


def cmd_invite(args: argparse.Namespace) -> int:
    for _ in range(max(1, args.count)):
        code = accounts.create_invite(args.note)
        print(code)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    row = _account_row(args.email)
    if not row:
        print("해당 이메일 계정이 없습니다.", file=sys.stderr)
        return 1
    print(f"id={row['id']}  {row['email']}  ({row['company'] or '-'})")
    print(f"플랜={row['plan']}  상태={row['status']}  가입={_fmt(row['created_at'])}")
    print(f"웹훅={'설정됨' if row['webhook'] else '없음'}")
    conds = accounts.conditions_for(row["id"])
    print(f"\n알림 조건 {len(conds)}개")
    for c in conds:
        print(f"  [{c['id']}] {c['name']} {'(사용)' if c['enabled'] else '(중지)'}")
        print(f"      키워드={', '.join(c['keywords']) or '-'}  "
              f"제외={', '.join(c['exclude_keywords']) or '-'}")
        print(f"      지역={', '.join(c['regions']) or '-'}  "
              f"분야={', '.join(c['categories']) or '-'}")
        print(f"      최소금액={c['min_amount']:,}  시점={','.join('D-' + str(s) for s in c['stages'])}")
    history = accounts.alert_history(row["id"], 10)
    print(f"\n최근 발송 {len(history)}건")
    for h in history:
        print(f"  {_fmt(h['sent_at'])}  {h['condition_name']}  D-{h['stage']}  "
              f"{h['bid_count']}건  {'성공' if h['ok'] else '실패'}  {h['detail']}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    row = _account_row(args.email)
    if not row:
        print("해당 이메일 계정이 없습니다.", file=sys.stderr)
        return 1
    fields: dict[str, str] = {}
    if args.company is not None:
        fields["company"] = args.company
    if args.name is not None:
        fields["name"] = args.name
    if args.phone is not None:
        fields["phone"] = args.phone
    if args.plan is not None:
        fields["plan"] = args.plan
    if args.status is not None:
        fields["status"] = args.status
    if args.webhook is not None:
        if args.webhook and not args.webhook.startswith("https://discord.com/api/webhooks/"):
            print("디스코드 웹훅 URL 형식이 아닙니다.", file=sys.stderr)
            return 1
        fields["webhook"] = args.webhook
    if not fields:
        print("변경할 항목이 없습니다. --help 참고.", file=sys.stderr)
        return 1
    accounts.update_account(row["id"], **fields)
    print(f"수정 완료: {row['email']} → {', '.join(fields)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="BidRadar 고객 계정 관리")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="계정 목록")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("add", help="계정 생성")
    p.add_argument("--email", required=True)
    p.add_argument("--password", help="생략하면 임시 비밀번호를 만듭니다")
    p.add_argument("--company", default="")
    p.add_argument("--name", default="")
    p.add_argument("--phone", default="")
    p.add_argument("--plan", default="pilot")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("reset", help="비밀번호 재설정")
    p.add_argument("--email", required=True)
    p.add_argument("--password", help="생략하면 임시 비밀번호를 만듭니다")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("invite", help="초대 코드 발급")
    p.add_argument("--note", default="")
    p.add_argument("--count", type=int, default=1)
    p.set_defaults(func=cmd_invite)

    p = sub.add_parser("show", help="계정 상세(조건·발송 이력)")
    p.add_argument("--email", required=True)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("set", help="계정 정보 수정")
    p.add_argument("--email", required=True)
    p.add_argument("--company")
    p.add_argument("--name")
    p.add_argument("--phone")
    p.add_argument("--plan")
    p.add_argument("--status", choices=("active", "paused"))
    p.add_argument("--webhook")
    p.set_defaults(func=cmd_set)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

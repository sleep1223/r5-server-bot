from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REASON_KEYS = ("REGION_LOCK_TO_HK", "REGION_LOCK_TO_MAINLAND")
LOCALES = ("zh", "en", "ja")
RESULT_ALIASES = {
    "p": "passed",
    "pass": "passed",
    "passed": "passed",
    "通过": "passed",
    "c": "crashed",
    "crash": "crashed",
    "crashed": "crashed",
    "崩溃": "crashed",
    "s": "skipped",
    "skip": "skipped",
    "skipped": "skipped",
    "跳过": "skipped",
    "q": "quit",
    "quit": "quit",
    "退出": "quit",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactively test runtime region-lock messages.")
    parser.add_argument("--cases", type=Path, default=Path("tools/region_lock_text_cases.json"), help="JSON test case file")
    parser.add_argument("--results", type=Path, default=Path("logs/region_lock_text_results.jsonl"), help="Append-only JSONL result file")
    parser.add_argument("--url", default="https://r5.sleep0.de/api/v1/r5/admin/region-lock-texts", help="Region-lock text API URL")
    parser.add_argument("--app-key", default=os.getenv("R5_ADMIN_APP_KEY"), help="Super-admin AppKey; defaults to R5_ADMIN_APP_KEY")
    parser.add_argument("--rerun", action="store_true", help="Run cases that already have a passed/crashed result")
    return parser.parse_args()


def load_cases(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as file:
        cases = json.load(file)
    if not isinstance(cases, list):
        raise ValueError("Test case file must contain a JSON array")

    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError(f"Case #{index} must be an object")
        case_id = str(case.get("id") or "").strip()
        locale = str(case.get("locale") or "").strip().lower()
        if not case_id or case_id in seen_ids:
            raise ValueError(f"Case #{index} has an empty or duplicate id")
        if locale not in LOCALES:
            raise ValueError(f"Case {case_id} has unsupported locale: {locale}")

        if "text" in case:
            text = str(case["text"]).strip()
            texts = {key: text for key in REASON_KEYS}
        else:
            raw_texts = case.get("texts")
            if not isinstance(raw_texts, dict) or not raw_texts:
                raise ValueError(f"Case {case_id} must provide text or texts")
            texts = {str(key): str(value).strip() for key, value in raw_texts.items()}
            unknown_keys = set(texts) - set(REASON_KEYS)
            if unknown_keys:
                raise ValueError(f"Case {case_id} has unsupported reason keys: {sorted(unknown_keys)}")
        if any(not text for text in texts.values()):
            raise ValueError(f"Case {case_id} contains an empty message")

        seen_ids.add(case_id)
        normalized.append({"id": case_id, "locale": locale, "texts": texts, "note": str(case.get("note") or "").strip()})
    return normalized


def completed_case_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    completed: set[str] = set()
    with path.open(encoding="utf-8") as file:
        for line in file:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            locale = str(record.get("locale") or "")
            updated_locales = set(record.get("updated_locales") or [])
            legacy_zh_result = locale == "zh" and not updated_locales
            all_locales_result = updated_locales == set(LOCALES)
            if record.get("result") in {"passed", "crashed"} and (legacy_zh_result or all_locales_result):
                completed.add(str(record.get("case_id") or ""))
    return completed


def update_server(url: str, app_key: str, case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    responses: dict[str, dict[str, Any]] = {}
    for locale in LOCALES:
        payload = json.dumps({"locale": locale, "texts": case["texts"]}, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=payload, method="PATCH", headers={"X-App-Key": app_key, "Content-Type": "application/json; charset=utf-8"})
        try:
            with urlopen(request, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"API returned HTTP {exc.code} while updating {locale}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"API request failed while updating {locale}: {exc.reason}") from exc
        if body.get("code") != "0000":
            raise RuntimeError(f"API rejected the {locale} update: {body}")
        responses[locale] = body
    return responses


def ask_result() -> str:
    while True:
        answer = input("结果 [p=通过/c=崩溃/s=跳过/q=退出]: ").strip().lower()
        result = RESULT_ALIASES.get(answer)
        if result:
            return result
        print("无法识别，请输入 p、c、s 或 q。")


def append_result(path: Path, case: dict[str, Any], result: str, responses: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "tested_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "case_id": case["id"],
        "locale": case["locale"],
        "texts": case["texts"],
        "note": case["note"],
        "result": result,
        "updated_locales": list(responses),
        "api_codes": {locale: response.get("code") for locale, response in responses.items()},
    }
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args()
    if not args.app_key:
        print("缺少 AppKey：请设置 R5_ADMIN_APP_KEY，或传入 --app-key。", file=sys.stderr)
        return 2

    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"无法加载测试文案：{exc}", file=sys.stderr)
        return 2

    completed = set() if args.rerun else completed_case_ids(args.results)
    pending = [case for case in cases if case["id"] not in completed]
    print(f"共 {len(cases)} 条，待测试 {len(pending)} 条，结果文件：{args.results}")

    for number, case in enumerate(pending, start=1):
        print(f"\n[{number}/{len(pending)}] {case['id']} locale={case['locale']}")
        for reason, text in case["texts"].items():
            print(f"  {reason}: {text}")
        try:
            responses = update_server(args.url, args.app_key, case)
        except RuntimeError as exc:
            print(f"服务端更新失败：{exc}", file=sys.stderr)
            return 1
        print(f"服务端 {', '.join(responses)} 四个语言槽位均已更新，请进入游戏测试。")

        result = ask_result()
        if result == "quit":
            print("已退出；下次运行会跳过已有通过/崩溃记录的用例。")
            return 0
        append_result(args.results, case, result, responses)
        print(f"已记录：{result}")

    print("全部待测试用例已完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

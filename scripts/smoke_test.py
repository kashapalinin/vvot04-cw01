#!/usr/bin/env python3
import argparse
import io
import json
import time
import uuid
import zipfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def request(base_url, path, *, body=None, expected_status=200):
    headers = {} if body is None else {"Content-Type": "application/json"}
    data = None if body is None else json.dumps(body).encode()
    try:
        response = urlopen(Request(base_url + path, data=data, headers=headers), timeout=30)
    except HTTPError as error:
        response = error

    method = "GET" if data is None else "POST"
    with response:
        content = response.read()
        if response.code != expected_status:
            raise RuntimeError(f"{method} {path}: expected {expected_status}, got {response.code}: {content[:500]!r}")
        if path != "/archives":
            print(f"{method} {path} → {response.code}", flush=True)
        return content, response.headers


def wait_for_gateway(api):
    deadline = time.monotonic() + 180
    while True:
        try:
            data, _ = request(api, "/archives")
            if not isinstance(json.loads(data), list):
                raise ValueError("GET /archives did not return a JSON array")
            print("API Gateway готов. GET /archives → 200.", flush=True)
            return
        except (RuntimeError, ValueError, URLError, TimeoutError) as error:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"API Gateway did not become ready: {error}") from error
            time.sleep(3)


def main():
    parser = argparse.ArgumentParser(description="Проверка API системы архивирования.")
    parser.add_argument("api_url", help="Служебный URL API-шлюза")
    parser.add_argument("source_url", nargs="?", help="Публичный URL небольшого файла со стабильным содержимым")
    parser.add_argument("--wait-ready", action="store_true", help="Дождаться успешного GET /archives")
    args = parser.parse_args()
    api = args.api_url.rstrip("/")
    if args.wait_ready:
        wait_for_gateway(api)
        return
    if not args.source_url:
        parser.error("source_url обязателен для проверки архивирования")

    with urlopen(args.source_url, timeout=30) as source:
        original = source.read()

    missing = f"{uuid.uuid4()}.zip"
    request(api, f"/archives/{missing}", expected_status=404)
    request(api, f"/download/{missing}", expected_status=404)
    request(api, "/tasks", body={"name": "demo.txt"}, expected_status=400)
    request(api, "/tasks", body={"name": "demo.txt", "url": "invalid"}, expected_status=400)

    task = {"name": f"demo-{uuid.uuid4().hex[:8]}.txt", "url": args.source_url}
    request(api, "/tasks", body=task, expected_status=202)
    print("GET /archives → ожидаем архив по имени", flush=True)
    deadline = time.monotonic() + 180
    while True:
        results, _ = request(api, "/archives")
        archives = json.loads(results)
        if not isinstance(archives, list):
            raise RuntimeError(f"Expected a JSON array, got {archives!r}")
        for item in archives:
            if item.get("name") == task["name"]:
                break
        else:
            if time.monotonic() >= deadline:
                raise TimeoutError("Archive did not appear in the list")
            time.sleep(3)
            continue
        break

    print(f"GET /archives → 200, найден {item['key']}", flush=True)
    key = item["key"]
    if not key.endswith(".zip") or uuid.UUID(key[:-4]).version != 7:
        raise RuntimeError(f"Expected a UUIDv7 ZIP key, got {key!r}")

    metadata, _ = request(api, f"/archives/{quote(key)}")
    expected_item = {**task, "key": key}
    if json.loads(metadata) != expected_item or item != expected_item:
        raise RuntimeError(f"Unexpected archive metadata: {metadata!r}")
    print(json.dumps(expected_item, ensure_ascii=False, indent=2), flush=True)

    content, headers = request(api, f"/download/{quote(key)}")
    if headers.get_content_type() != "application/zip":
        raise RuntimeError(f"Unexpected Content-Type: {headers.get('Content-Type')!r}")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if archive.namelist() != [task["name"]] or archive.read(task["name"]) != original:
            raise RuntimeError("Archive contents do not match the source file")
    print(f"ZIP проверен. Файл внутри совпадает с источником ({len(original)} байт).", flush=True)


if __name__ == "__main__":
    main()

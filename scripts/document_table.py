import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def document_request(action, payload, *, accepted_errors=()):
    request = Request(os.environ["YDB_ENDPOINT"], json.dumps(payload).encode(), headers={
        "Authorization": f"Bearer {os.environ['YC_TOKEN']}",
        "Content-Type": "application/x-amz-json-1.0",
        "X-Amz-Target": f"DynamoDB_20120810.{action}",
    })

    for attempt in range(8):
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            with error:
                detail = error.read().decode(errors="replace")
            try:
                code = json.loads(detail).get("__type", "").rsplit("#", 1)[-1]
            except (ValueError, AttributeError):
                code = ""
            if code in accepted_errors:
                return {}
            if (error.code not in {429, 500, 502, 503, 504} and
                    code not in {"ThrottlingException", "LimitExceededException"}) or attempt == 7:
                raise RuntimeError(f"YDB {action}: HTTP {error.code}, {detail}") from error
        except (URLError, TimeoutError):
            if attempt == 7:
                raise
        time.sleep(min(2 ** attempt, 8))


def main():
    table_name = os.environ["TABLE_NAME"]
    action, = sys.argv[1:]
    table = {"TableName": table_name}

    if action == "create":
        schema = [{"AttributeName": "key", "KeyType": "HASH"}]
        attributes = [{"AttributeName": "key", "AttributeType": "S"}]
        document_request("CreateTable", {
            **table,
            "BillingMode": "PAY_PER_REQUEST",
            "KeySchema": schema,
            "AttributeDefinitions": attributes,
        }, accepted_errors=("ResourceInUseException",))
        for _ in range(30):
            description = document_request("DescribeTable", table, accepted_errors=("ResourceNotFoundException",)).get("Table")
            if description and description["TableStatus"] == "ACTIVE":
                break
            time.sleep(2)
        else:
            raise TimeoutError(f"YDB table {table_name} did not become active")
        if description["KeySchema"] != schema or description["AttributeDefinitions"] != attributes:
            raise RuntimeError(f"Unexpected key schema in {table_name}")
        print(f"YDB document table {table_name} is ready")
    elif action == "delete":
        document_request("DeleteTable", table, accepted_errors=("ResourceNotFoundException",))
        print(f"YDB document table {table_name} deleted")
    else:
        raise ValueError(f"Unexpected action {action}")


if __name__ == "__main__":
    main()

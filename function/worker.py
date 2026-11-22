import hashlib
import http.client
import ipaddress
import json
import os
import shutil
import socket
import ssl
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen


HTTP_TIMEOUT = 30
MAX_REDIRECTS = 5
TLS_CONTEXT = ssl.create_default_context()


class PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, secure):
        self.default_port = 443 if secure else 80
        super().__init__(host, port, timeout=HTTP_TIMEOUT)
        self.secure = secure

    def connect(self):
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        for _, _, _, _, address in addresses:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global or ip.is_multicast or getattr(ip, "is_site_local", False):
                raise ValueError("The source must resolve to a public unicast address")

        # Connect to the checked addresses while keeping the original HTTP host and TLS name.
        last_error = None
        for family, kind, protocol, _, address in addresses:
            raw_socket = None
            try:
                raw_socket = socket.socket(family, kind, protocol)
                raw_socket.settimeout(self.timeout)
                raw_socket.connect(address)
                self.sock = (
                    TLS_CONTEXT.wrap_socket(raw_socket, server_hostname=self.host)
                    if self.secure else raw_socket
                )
                return
            except OSError as error:
                if raw_socket is not None:
                    raw_socket.close()
                last_error = error
        raise OSError("Could not connect to the public source") from last_error


@contextmanager
def open_source(url):
    for _ in range(MAX_REDIRECTS + 1):
        if any(character.isspace() or ord(character) < 32 for character in url):
            raise ValueError("Source URL contains whitespace or control characters")
        target = urlsplit(url)
        if target.scheme not in {"http", "https"} or not target.hostname or "@" in target.netloc:
            raise ValueError("Expected an HTTP or HTTPS source URL without credentials")

        secure = target.scheme == "https"
        host = target.hostname.encode("idna").decode("ascii")
        path = quote(target.path or "/", safe="/:@!$&'()*+,;=-._~%")
        if target.query:
            path += "?" + quote(target.query, safe="/:?@!$&'()*+,;=-._~%")
        connection = PublicHTTPConnection(host, target.port or (443 if secure else 80), secure)
        try:
            connection.request("GET", path, headers={"Accept-Encoding": "identity"})
            with connection.getresponse() as response:
                if 200 <= response.status < 300 and response.status != 206:
                    yield response
                    return
                location = response.getheader("Location")
                if response.status not in {301, 302, 303, 307, 308} or not location:
                    raise OSError(f"Source returned HTTP {response.status}")
                url = urljoin(url, location)
        finally:
            connection.close()
    raise OSError("Too many redirects from the source")


def archive_key(message):
    timestamp = int(message["attributes"]["SentTimestamp"])
    # Keep one UUIDv7 per queue message even if YMQ delivers it more than once.
    entropy = hashlib.sha256(message["message_id"].encode()).digest()[:10]
    random_bits = int.from_bytes(entropy, "big") >> 6
    value = (timestamp << 80) | ((random_bits >> 62) << 64) | (random_bits & ((1 << 62) - 1))
    return f"{uuid.UUID(int=value, version=7)}.zip"


def handler(event, context):
    message, = event["messages"]
    message = message["details"]["message"]
    task = json.loads(message["body"])
    name = task["name"]
    url = task["url"]
    if not name or name in {".", ".."} or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("Expected a single filename")

    key = archive_key(message)
    authorization = {"Authorization": f"Bearer {context.token['access_token']}"}
    object_url = f"https://storage.yandexcloud.net/{os.environ['BUCKET_NAME']}/{key}"

    with tempfile.TemporaryFile() as archive:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            with open_source(url) as source, zipped.open(name, "w", force_zip64=True) as entry:
                shutil.copyfileobj(source, entry, length=64 * 1024)
                if source.length:
                    raise OSError("Source closed before the complete file was received")

        size = archive.tell()
        archive.seek(0)
        upload = Request(object_url, archive, method="PUT", headers={
            **authorization,
            "Content-Length": str(size),
            "Content-Type": "application/zip",
        })
        with urlopen(upload, timeout=HTTP_TIMEOUT):
            pass

    item = {field: {"S": value} for field, value in {"name": name, "url": url, "key": key}.items()}
    put_item = Request(os.environ["YDB_ENDPOINT"], json.dumps({
        "TableName": os.environ["TABLE_NAME"],
        "Item": item,
    }).encode(), method="POST", headers={
        **authorization,
        "Content-Type": "application/x-amz-json-1.0",
        "X-Amz-Target": "DynamoDB_20120810.PutItem",
    })
    with urlopen(put_item, timeout=HTTP_TIMEOUT):
        pass

    print(f"Saved archive {key}")

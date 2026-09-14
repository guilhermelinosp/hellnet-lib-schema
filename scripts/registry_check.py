"""Apicurio v2 rule test: no artifact or rule configuration is changed."""
import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from contracts import FILES, MODES, read_json, require, validate_document


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Registry redirects are refused; supply the final endpoint")


def request(url, method="GET", data=None, content_type="application/json"):
    headers = {"Content-Type": content_type}
    if os.environ.get("APICURIO_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["APICURIO_TOKEN"]
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        raise ValueError(f"Registry HTTP {error.code}; no compatibility success established") from error


def check(registry, group, directory):
    parsed = urllib.parse.urlsplit(registry)
    require(parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.query and not parsed.fragment
            and not parsed.username, "invalid Registry base URL")
    directory = Path(directory)
    meta = read_json(directory / ".meta.json")
    require(meta.get("type") in FILES and meta.get("compatibility") in MODES, "invalid metadata")
    file = directory / FILES[meta["type"]]
    validate_document(file, meta["type"])
    artifact = (registry.rstrip("/") + "/apis/registry/v2/groups/" + urllib.parse.quote(group, safe="")
                + "/artifacts/" + urllib.parse.quote(meta["name"], safe=""))
    status, body = request(artifact + "/rules/COMPATIBILITY")
    require(status == 200, "could not read the artifact compatibility rule")
    rule = json.loads(body)
    require(rule.get("config") == meta["compatibility"],
            "Registry compatibility rule differs or is absent; configure it explicitly before testing")
    content_type = {"avro": "application/vnd.apache.avro+json", "json": "application/json",
                    "protobuf": "application/x-protobuf"}[meta["type"]]
    # Apicurio's PUT /test is a non-mutating rule test, not PUT /artifacts.
    status, _ = request(artifact + "/test", "PUT", file.read_bytes(), content_type)
    require(status == 204, "unexpected Registry test response")
    print(f"Registry rules passed ({meta['compatibility']}); no content or rules changed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--group", default="default")
    parser.add_argument("--schema", required=True, type=Path)
    args = parser.parse_args()
    try:
        check(args.registry, args.group, args.schema)
    except Exception as error:
        parser.exit(1, f"ERROR: {error}\n")


if __name__ == "__main__":
    main()

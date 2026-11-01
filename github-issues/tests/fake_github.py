"""A small stand-in for the parts of GitHub's REST and GraphQL APIs prized_issues.py calls.

Tokens map to users; one fine-grained token (no X-OAuth-Scopes header) and one classic token (with it).
Issues, comments, labels and pull requests live in memory; pull requests check their branch exists in
the local bare repo that stands in for GitHub's git side.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def stamp(offset: int = 0) -> str:
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Fake:
    def __init__(self, bare: str, full: str = "acme/app"):
        self.full = full
        self.bare = bare
        self.users = {"owner": 1, "teammate": 2, "outsider": 3, "triager": 4}
        self.tokens = {"github_pat_fine": ("owner", None), "gho_classic": ("owner", "repo, workflow")}
        self.issues: dict[int, dict] = {}
        self.comments: list[dict] = []
        self.pulls: dict[int, dict] = {}
        self.labels: set[str] = set()
        self.next_number = 1
        self.next_id = 1000
        self.requests: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    # --- what a test does on "GitHub" ---

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def open_issue(self, title: str, body: str, author: str = "owner") -> int:
        with self.lock:
            n = self.next_number
            self.next_number += 1
            self.issues[n] = {
                "title": title,
                "body": body,
                "author": author,
                "labels": [],
                "timeline": [],
                "edits": [],
                "lastEditedAt": None,
                "updated_at": stamp(),
                "state": "open",
            }
            return n

    def label(self, n: int, name: str = "prized", actor: str = "owner", at: str | None = None) -> None:
        with self.lock:
            i = self.issues[n]
            if name not in i["labels"]:
                i["labels"].append(name)
            i["timeline"].append(
                {
                    "__typename": "LabeledEvent",
                    "id": f"LE_{self._id()}",
                    "createdAt": at or stamp(),
                    "actor": actor,
                    "label": name,
                }
            )
            i["updated_at"] = stamp()

    def unlabel(self, n: int, name: str = "prized") -> None:
        with self.lock:
            self.issues[n]["labels"].remove(name)
            self.issues[n]["updated_at"] = stamp()

    def edit(self, n: int, body: str, editor: str, at: str | None = None) -> None:
        with self.lock:
            i = self.issues[n]
            i["body"] = body
            i["edits"].append(editor)
            i["lastEditedAt"] = at or stamp()
            i["updated_at"] = stamp()

    def rename(self, n: int, title: str, actor: str, at: str | None = None) -> None:
        with self.lock:
            i = self.issues[n]
            i["title"] = title
            i["timeline"].append({"__typename": "RenamedTitleEvent", "createdAt": at or stamp(), "actor": actor})
            i["updated_at"] = stamp()

    def comment(self, n: int, body: str, author: str = "owner", edited: bool = False) -> dict:
        with self.lock:
            c = {
                "id": self._id(),
                "n": n,
                "user": author,
                "body": body,
                "created_at": stamp(),
                "updated_at": stamp(5) if edited else stamp(),
            }
            self.comments.append(c)
            (self.issues.get(n) or self.pulls.get(n) or {})["updated_at"] = stamp()
            return c

    def comments_on(self, n: int) -> list[str]:
        return [c["body"] for c in self.comments if c["n"] == n]

    def bot_comments(self, n: int) -> list[str]:
        """Comments the poller posted (as the token's owner) that aren't /prized commands."""
        return [c["body"] for c in self.comments if c["n"] == n and c.get("by_poller")]

    # --- the API ---

    def user(self, login: str) -> dict:
        return {"login": login, "id": self.users[login], "type": "User"}

    def actor(self, login: str) -> dict:
        return {"login": login, "databaseId": self.users[login]}

    def graphql_issue(self, n: int) -> dict | None:
        i = self.issues.get(n)
        if not i:
            return None
        renames = [
            {"createdAt": e["createdAt"], "actor": self.actor(e["actor"])}
            for e in i["timeline"]
            if e["__typename"] == "RenamedTitleEvent"
        ]
        labels = [
            {"id": e["id"], "createdAt": e["createdAt"], "actor": self.actor(e["actor"]), "label": {"name": e["label"]}}
            for e in i["timeline"]
            if e["__typename"] == "LabeledEvent"
        ]
        edits = [{"editor": self.actor(e)} for e in i["edits"]]
        return {
            "title": i["title"],
            "bodyText": i["body"],
            "lastEditedAt": i["lastEditedAt"],
            "state": i["state"].upper(),
            "author": self.actor(i["author"]),
            # Like GitHub: first/last N of each list, with the full count beside it.
            "userContentEdits": {"totalCount": len(edits), "nodes": edits[:100]},
            "renames": {"totalCount": len(renames), "nodes": renames[:100]},
            "labels": {"nodes": labels[-50:]},
        }

    @staticmethod
    def page(items: list, query: dict, default_sort: str) -> list:
        """GitHub's list behaviour: sort (created or updated), direction, and one page of per_page items."""
        key = {"created": "created_at", "updated": "updated_at"}.get(query.get("sort", [""])[0], default_sort)
        items = sorted(items, key=lambda x: (x.get(key, ""), x.get("id", x.get("number", 0))))
        if query.get("direction", ["asc"])[0] == "desc":
            items.reverse()
        return items[: int(query.get("per_page", ["30"])[0])]

    def branch_exists(self, branch: str) -> bool:
        r = subprocess.run(
            ["git", "-C", self.bare, "rev-parse", "--verify", f"refs/heads/{branch}"], capture_output=True
        )
        return r.returncode == 0

    def handle(self, method: str, path: str, query: dict, body, token: str):
        self.requests.append((method, path))
        if token not in self.tokens:
            return 401, {"message": "Bad credentials"}, {}
        me, scopes = self.tokens[token]
        extra = {"X-OAuth-Scopes": scopes} if scopes is not None else {}
        base = f"/repos/{self.full}"
        with self.lock:
            if path == "/user":
                return 200, self.user(me), extra
            if path.startswith("/users/"):
                login = path.rsplit("/", 1)[1]
                return (200, self.user(login), {}) if login in self.users else (404, {"message": "Not Found"}, {})
            if path == base:
                return 200, {"full_name": self.full, "default_branch": "main"}, {}
            if path == f"{base}/labels" and method == "POST":
                if body["name"] in self.labels:
                    return 422, {"message": "Validation Failed"}, {}
                self.labels.add(body["name"])
                return 201, {"name": body["name"]}, {}
            if path == f"{base}/issues" and method == "GET":
                label = query.get("labels", [""])[0]
                out = [
                    {"number": n, "updated_at": i["updated_at"]}
                    for n, i in sorted(self.issues.items())
                    if label in i["labels"] and i["state"] == "open"
                ]
                out += [{"number": n, "updated_at": p["updated_at"], "pull_request": {}} for n, p in self.pulls.items()]
                return 200, self.page(out, query, "updated_at"), {}
            if path == f"{base}/issues/comments" and method == "GET":
                since = query.get("since", [""])[0]
                out = [
                    {
                        "id": c["id"],
                        "issue_url": f"https://api.github.com{base}/issues/{c['n']}",
                        "user": self.user(c["user"]),
                        "body": c["body"],
                        "created_at": c["created_at"],
                        "updated_at": c["updated_at"],
                    }
                    for c in self.comments
                    if c["updated_at"] >= since
                ]
                return 200, self.page(out, query, "created_at"), {}
            if path.startswith(f"{base}/issues/") and path.endswith("/comments") and method == "POST":
                n = int(path.split("/")[-2])
                c = {
                    "id": self._id(),
                    "n": n,
                    "user": me,
                    "body": body["body"],
                    "created_at": stamp(),
                    "updated_at": stamp(),
                    "by_poller": True,
                }
                self.comments.append(c)
                target = self.issues.get(n) or self.pulls.get(n)
                if target is not None:
                    target["updated_at"] = stamp()
                return 201, {"id": c["id"]}, {}
            if path == f"{base}/pulls" and method == "POST":
                if not self.branch_exists(body["head"]):
                    return 422, {"message": f"No commits between main and {body['head']}"}, {}
                n = self.next_number
                self.next_number += 1
                self.pulls[n] = {**body, "number": n, "state": "open", "updated_at": stamp()}
                return 201, {"number": n, "html_url": f"https://github.com/{self.full}/pull/{n}"}, {}
            if path.startswith(f"{base}/pulls/") and method == "GET":
                n = int(path.rsplit("/", 1)[1])
                p = self.pulls.get(n)
                return (200, {"number": n, "state": p["state"]}, {}) if p else (404, {"message": "Not Found"}, {})
            if path == "/graphql":
                n = body["variables"]["n"]
                return 200, {"data": {"repository": {"issue": self.graphql_issue(n)}}}, {}
        return 404, {"message": f"no route {method} {path}"}, {}


def serve(fake: Fake) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _go(self):
            u = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            status, out, headers = fake.handle(self.command, u.path, urllib.parse.parse_qs(u.query), body, token)
            raw = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _go

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"

"""Shared helpers for the NCF dashboard regeneration script.

Loads credentials, talks to Metabase / Mixpanel / Typeform / Customer.io.
Credentials come from environment variables (see load_secrets); in CI they are
injected as CI/CD variables, locally they are read from ~/.config/secrets.env.
"""
import os
import re
import json
import base64
import urllib.request
import urllib.parse
import urllib.error

SECRETS_PATH = os.path.expanduser("~/.config/secrets.env")


def load_secrets(path=SECRETS_PATH):
    """Populate os.environ from a KEY=VALUE file if it exists (local dev).

    In CI the variables are already in the environment, so a missing file is
    fine. Handles optional `export ` prefix and surrounding quotes.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            os.environ.setdefault(key, val)


_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _request(url, data=None, headers=None, method=None, timeout=60):
    headers = dict(headers or {})
    headers.setdefault("User-Agent", _UA)
    headers.setdefault("Accept", "application/json, text/plain, */*")
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} for {url}: {raw[:400]}") from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


# ---------------------------------------------------------------- Metabase ---
class Metabase:
    def __init__(self):
        self.url = os.environ["METABASE_URL"].rstrip("/")
        self.user = os.environ["METABASE_USERNAME"]
        self.pw = os.environ["METABASE_PASSWORD"]
        self._token = None

    def _session(self):
        if self._token:
            return self._token
        out = _request(
            f"{self.url}/api/session",
            data={"username": self.user, "password": self.pw},
        )
        self._token = out["id"]
        return self._token

    def query(self, database_id, sql):
        """Run native SQL, return list-of-dicts."""
        out = _request(
            f"{self.url}/api/dataset",
            data={
                "database": database_id,
                "type": "native",
                "native": {"query": sql},
            },
            headers={"X-Metabase-Session": self._session()},
        )
        data = out.get("data", {})
        cols = [c["name"] for c in data.get("cols", [])]
        rows = data.get("rows", [])
        return [dict(zip(cols, r)) for r in rows]

    def one(self, database_id, sql):
        """Run SQL expected to return a single row; return that dict (or {})."""
        rows = self.query(database_id, sql)
        return rows[0] if rows else {}


# ---------------------------------------------------------------- Mixpanel ---
class Mixpanel:
    """EU-residency Query API via service-account basic auth."""

    def __init__(self):
        self.user = os.environ["MIXPANEL_SA_USERNAME"]
        self.secret = os.environ["MIXPANEL_SA_SECRET"]
        self.project_id = os.environ["MIXPANEL_PROJECT_ID"]
        tok = base64.b64encode(f"{self.user}:{self.secret}".encode()).decode()
        self.auth = "Basic " + tok

    def segmentation(self, event, from_date, to_date, unit="day", where=None, typ="general"):
        params = {
            "project_id": self.project_id,
            "event": event,
            "from_date": from_date,
            "to_date": to_date,
            "unit": unit,
            "type": typ,   # general = event count (page views), unique = distinct users
        }
        if where:
            params["where"] = where
        url = "https://eu.mixpanel.com/api/2.0/segmentation?" + urllib.parse.urlencode(params)
        return _request(url, headers={"Authorization": self.auth})


# ---------------------------------------------------------------- Typeform ---
class Typeform:
    def __init__(self):
        self.token = os.environ["TYPEFORM_TOKEN"]
        self.h = {"Authorization": "Bearer " + self.token}

    def get(self, path, **params):
        url = "https://api.typeform.com" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _request(url, headers=self.h)


# ------------------------------------------------------------- Customer.io ---
class CustomerIO:
    """App API (Bearer app key). Tries US then EU base."""

    BASES = ["https://api.customer.io/v1", "https://api-eu.customer.io/v1"]

    def __init__(self):
        self.key = os.environ["CIO_APP_API_KEY"]
        self.h = {"Authorization": "Bearer " + self.key}
        self.base = None

    def get(self, path, **params):
        bases = [self.base] if self.base else self.BASES
        last = None
        for base in bases:
            url = base + path
            if params:
                url += "?" + urllib.parse.urlencode(params)
            try:
                out = _request(url, headers=self.h)
                self.base = base
                return out
            except RuntimeError as e:
                last = e
        raise last


if __name__ == "__main__":
    # smoke test
    load_secrets()
    mb = Metabase()
    r = mb.one(14, "select count(*) n from users")
    print("metabase ok, users:", r)

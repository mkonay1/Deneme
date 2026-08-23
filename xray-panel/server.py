#!/usr/bin/env python3
"""Xray test kosum paneli - lokal sunucu.

Tarayici CORS nedeniyle jira.thy.com'a dogrudan istek atamaz;
bu sunucu arayuzu servis eder ve Jira/Xray API cagrilarini yapar.

Calistirma:  python3 server.py          (port 8765)
Token:       Panel ilk acildiginda arayuzde sorulur; yalnizca bu oturumda
             bellekte tutulur, diske kaydedilmez (her acilista tekrar istenir).
             Istege bagli olarak JIRA_TOKEN ortam degiskeniyle de verilebilir.
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = os.environ.get("JIRA_BASE", "https://jira.thy.com")
PORT = int(os.environ.get("PORT", "8765"))
ROOT = Path(__file__).resolve().parent
PAGE_LIMIT = 200
WORKERS = 3
MAX_RETRIES = 6
CACHE_TTL = 600  # saniye

_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

# Token yalnizca bellekte tutulur; diske yazilmaz. Sunucu kapaninca silinir,
# boylece panel her acildiginda token yeniden istenir.
_session_token = ""
_token_lock = threading.Lock()


def get_token() -> str:
    with _token_lock:
        if _session_token:
            return _session_token
    return os.environ.get("JIRA_TOKEN", "").strip()


def set_token(tok: str) -> None:
    global _session_token
    with _token_lock:
        _session_token = (tok or "").strip()
    # Yeni token baska bir kullaniciya ait olabilir; onbellegi temizle.
    with _cache_lock:
        _cache.clear()


def jira_get(path: str, token: str, params: dict | None = None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES - 1:
                retry_after = e.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                time.sleep(wait + random.uniform(0, 0.5))
                continue
            raise


def parse_dt(value) -> datetime | None:
    """Xray finishedOn alani ISO string, epoch ms veya Jira tarih formati olabilir."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000).astimezone()
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone() if dt.tzinfo else dt
    except ValueError:
        pass
    for fmt in ("%d/%b/%y %I:%M %p", "%d/%b/%Y %I:%M %p", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def classify(status: str) -> str:
    s = (status or "").upper()
    if s == "PASS":
        return "pass"
    if s == "FAIL":
        return "fail"
    return "other"


def fetch_execution_runs(exec_key: str, d_from: date, d_to: date, token: str) -> tuple[list, dict]:
    """Bir execution icin: aralikta bitmis kosumlar (gun_iso, userKey, status)
    + tarihten bagimsiz guncel durum sayaclari (execution ilerlemesi)."""
    found: list[tuple] = []
    stats = {"total": 0, "pass": 0, "fail": 0, "todo": 0, "other": 0}
    page = 1
    while True:
        tests = jira_get(
            f"/rest/raven/1.0/api/testexec/{exec_key}/test",
            token,
            {"limit": PAGE_LIMIT, "page": page, "detailed": "true"},
        )
        if not tests:
            break
        for t in tests:
            stats["total"] += 1
            st = str(t.get("status") or "").upper()
            if st == "PASS":
                stats["pass"] += 1
            elif st == "FAIL":
                stats["fail"] += 1
            elif st == "TODO":
                stats["todo"] += 1
            else:
                stats["other"] += 1
            fin, by = t.get("finishedOn"), t.get("executedBy")
            if fin and by:
                dt = parse_dt(fin)
                if dt and d_from <= dt.date() <= d_to:
                    found.append((dt.date().isoformat(), str(by), str(t.get("status") or "")))
        if len(tests) < PAGE_LIMIT:
            break
        page += 1
    return found, stats


def fetch_plan_progress(plan: str, token: str) -> tuple[int, int]:
    """Plandaki toplam test sayisi ve son durumu TODO olmayan (kosulmus) test sayisi."""
    total = done = 0
    page = 1
    while True:
        tests = jira_get(
            f"/rest/raven/1.0/api/testplan/{plan}/test",
            token,
            {"limit": PAGE_LIMIT, "page": page},
        )
        if not tests:
            break
        for t in tests:
            total += 1
            st = str(t.get("latestStatus") or "").upper()
            if st and st != "TODO":
                done += 1
        if len(tests) < PAGE_LIMIT:
            break
        page += 1
    return total, done


def resolve_user(user_key: str, token: str) -> tuple[str, str]:
    for param in ("username", "key"):
        try:
            u = jira_get("/rest/api/2/user", token, {param: user_key})
            if u.get("displayName"):
                return user_key, u["displayName"]
        except Exception:
            continue
    return user_key, user_key


def collect(plan: str, d_from: date, d_to: date, token: str) -> dict:
    executions = jira_get(f"/rest/raven/1.0/api/testplan/{plan}/testexecution", token)
    exec_keys = [e["key"] for e in executions if e.get("key")]
    summaries = {e["key"]: e.get("summary") or "" for e in executions if e.get("key")}

    runs: list[tuple] = []
    exec_list: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fut_map = {pool.submit(fetch_execution_runs, k, d_from, d_to, token): k for k in exec_keys}
        for fut in as_completed(fut_map):
            key = fut_map[fut]
            part, stats = fut.result()
            runs.extend(part)
            done = stats["total"] - stats["todo"]
            exec_list.append({"key": key, "summary": summaries.get(key, ""), "done": done, **stats})
    # testexecution yaniti summary icermiyorsa tek JQL sorgusuyla tamamla
    if exec_list and not any(e["summary"] for e in exec_list):
        try:
            r = jira_get("/rest/api/2/search", token, {
                "jql": "key in (%s)" % ",".join(e["key"] for e in exec_list),
                "fields": "summary",
                "maxResults": len(exec_list),
            })
            found = {i["key"]: i["fields"].get("summary", "") for i in r.get("issues", [])}
            for e in exec_list:
                e["summary"] = found.get(e["key"], "")
        except Exception:
            pass
    exec_list.sort(key=lambda e: ((e["done"] / e["total"]) if e["total"] else 1.0, e["key"]))

    plan_total, plan_done = fetch_plan_progress(plan, token)

    per_person: dict[str, dict] = {}
    per_day: dict[str, dict] = {}
    for day_iso, user, status in runs:
        cls = classify(status)
        p = per_person.setdefault(user, {"total": 0, "pass": 0, "fail": 0, "other": 0})
        p["total"] += 1
        p[cls] += 1
        d = per_day.setdefault(day_iso, {"total": 0, "pass": 0, "fail": 0, "other": 0})
        d["total"] += 1
        d[cls] += 1

    names: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(resolve_user, k, token) for k in per_person]
        for fut in as_completed(futures):
            key, name = fut.result()
            names[key] = name

    people = sorted(
        ({"key": k, "name": names.get(k, k), **v} for k, v in per_person.items()),
        key=lambda x: (-x["total"], x["name"].lower()),
    )

    days = []
    cur = d_from
    while cur <= d_to:
        iso = cur.isoformat()
        entry = per_day.get(iso, {"total": 0, "pass": 0, "fail": 0, "other": 0})
        days.append({"date": iso, **entry})
        cur += timedelta(days=1)

    total = len(runs)
    n_days = (d_to - d_from).days + 1
    rate = total / n_days
    remaining = max(plan_total - plan_done, 0)
    estimate = math.ceil(remaining / rate) if rate > 0 and remaining > 0 else None

    return {
        "plan": plan,
        "from": d_from.isoformat(),
        "to": d_to.isoformat(),
        "executions": len(exec_keys),
        "total": total,
        "passCount": sum(p["pass"] for p in people),
        "failCount": sum(p["fail"] for p in people),
        "otherCount": sum(p["other"] for p in people),
        "people": people,
        "days": days,
        "execProgress": exec_list,
        "progress": {
            "total": plan_total,
            "done": plan_done,
            "remaining": remaining,
            "estimateDays": estimate,
        },
        "generatedAt": datetime.now().strftime("%H:%M"),
    }


def demo_data(d_from: date, d_to: date) -> dict:
    people_names = [
        "Ayşe Yılmaz", "Mehmet Demir", "Zeynep Kaya",
        "Emre Şahin", "Elif Çelik", "Burak Arslan",
    ]
    rng = random.Random(f"{d_from}{d_to}")
    per_person = {n: {"total": 0, "pass": 0, "fail": 0, "other": 0} for n in people_names}
    days = []
    cur = d_from
    while cur <= d_to:
        day = {"date": cur.isoformat(), "total": 0, "pass": 0, "fail": 0, "other": 0}
        weekend = cur.weekday() >= 5
        for name in people_names:
            n = 0 if weekend else rng.randint(0, 14)
            for _ in range(n):
                r = rng.random()
                cls = "pass" if r < 0.72 else ("fail" if r < 0.9 else "other")
                per_person[name]["total"] += 1
                per_person[name][cls] += 1
                day["total"] += 1
                day[cls] += 1
        days.append(day)
        cur += timedelta(days=1)
    people = sorted(
        ({"key": n, "name": n, **v} for n, v in per_person.items() if v["total"]),
        key=lambda x: (-x["total"], x["name"]),
    )
    total = sum(p["total"] for p in people)
    n_days = (d_to - d_from).days + 1
    done, plan_total = 128, 196
    rate = total / n_days
    exec_list = []
    for i in range(1, 8):
        t = rng.randint(8, 60)
        p_ = rng.randint(0, t)
        f_ = rng.randint(0, t - p_) if t - p_ else 0
        o_ = rng.randint(0, min(3, t - p_ - f_)) if t - p_ - f_ else 0
        exec_list.append({
            "key": f"TKP3576-{29900 + i}", "summary": f"Demo Execution {i} — Regresyon Paketi",
            "total": t, "pass": p_, "fail": f_, "other": o_,
            "todo": t - p_ - f_ - o_, "done": p_ + f_ + o_,
        })
    exec_list.sort(key=lambda e: e["done"] / e["total"] if e["total"] else 1.0)
    return {
        "plan": "TKP3576-29946", "from": d_from.isoformat(), "to": d_to.isoformat(),
        "executions": 14, "total": total, "execProgress": exec_list,
        "passCount": sum(p["pass"] for p in people),
        "failCount": sum(p["fail"] for p in people),
        "otherCount": sum(p["other"] for p in people),
        "people": people, "days": days,
        "progress": {
            "total": plan_total, "done": done, "remaining": plan_total - done,
            "estimateDays": math.ceil((plan_total - done) / rate) if rate else None,
        },
        "generatedAt": datetime.now().strftime("%H:%M"),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def send_result(self, data: dict, want_pptx: bool):
        if not want_pptx:
            self.send_json(data)
            return
        try:
            from pptx_report import build_pptx
        except ImportError:
            self.send_json({"error": "python-pptx kurulu değil (pip install python-pptx)."}, 501)
            return
        body = build_pptx(data)
        fname = f"xray-{data['plan']}-{data['from']}_{data['to']}.pptx"
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/token":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                self.send_json({"ok": False, "reason": "bad-request"}, 400)
                return
            tok = str(payload.get("token") or "").strip()
            if not tok:
                self.send_json({"ok": False, "reason": "empty"}, 400)
                return
            try:
                me = jira_get("/rest/api/2/myself", tok)
            except urllib.error.HTTPError as e:
                self.send_json({"ok": False, "reason": "auth-failed", "detail": f"HTTP {e.code}"})
                return
            except Exception as e:
                self.send_json({"ok": False, "reason": "network", "detail": str(e)})
                return
            set_token(tok)
            self.send_json({"ok": True, "user": me.get("displayName") or me.get("name")})
            return

        if parsed.path == "/api/logout":
            set_token("")
            self.send_json({"ok": True})
            return

        self.send_json({"error": "not found"}, 404)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/status":
            token = get_token()
            if not token:
                self.send_json({"ok": False, "reason": "token-missing"})
                return
            try:
                me = jira_get("/rest/api/2/myself", token)
                self.send_json({"ok": True, "user": me.get("displayName") or me.get("name")})
            except urllib.error.HTTPError as e:
                self.send_json({"ok": False, "reason": "auth-failed", "detail": f"HTTP {e.code}"})
            except Exception as e:
                self.send_json({"ok": False, "reason": "network", "detail": str(e)})
            return

        if parsed.path == "/api/plans":
            token = get_token()
            if not token:
                self.send_json({"error": "token yok"}, 401)
                return
            project = qs.get("project", ["TKP3576"])[0].strip() or "TKP3576"
            try:
                r = jira_get("/rest/api/2/search", token, {
                    "jql": f'project = "{project}" AND issuetype = "Test Plan" ORDER BY updated DESC',
                    "fields": "summary",
                    "maxResults": 100,
                })
                plans = [
                    {"key": i["key"], "summary": i["fields"].get("summary", "")}
                    for i in r.get("issues", [])
                ]
                self.send_json({"plans": plans})
            except Exception as e:
                self.send_json({"error": str(e)}, 502)
            return

        if parsed.path in ("/api/runs", "/api/report.pptx"):
            want_pptx = parsed.path == "/api/report.pptx"
            try:
                d_from = date.fromisoformat(qs.get("from", [date.today().isoformat()])[0])
                d_to = date.fromisoformat(qs.get("to", [d_from.isoformat()])[0])
            except ValueError:
                self.send_json({"error": "Geçersiz tarih."}, 400)
                return
            if d_from > d_to:
                d_from, d_to = d_to, d_from

            if qs.get("demo", [""])[0]:
                self.send_result(demo_data(d_from, d_to), want_pptx)
                return

            token = get_token()
            if not token:
                self.send_json({"error": "Token bulunamadı. JIRA_TOKEN ortam değişkenini ayarlayın veya ~/.jira_token dosyası oluşturun."}, 401)
                return
            plan = qs.get("plan", [""])[0].strip()
            if not plan:
                self.send_json({"error": "Test plan key'i gerekli."}, 400)
                return

            cache_key = (plan, d_from.isoformat(), d_to.isoformat())
            force = bool(qs.get("refresh", [""])[0])
            if not force:
                with _cache_lock:
                    hit = _cache.get(cache_key)
                if hit and time.time() - hit[0] < CACHE_TTL:
                    data = dict(hit[1])
                    data["cached"] = True
                    self.send_result(data, want_pptx)
                    return
            try:
                data = collect(plan, d_from, d_to, token)
                with _cache_lock:
                    _cache[cache_key] = (time.time(), data)
                self.send_result(data, want_pptx)
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                self.send_json({"error": f"Jira HTTP {e.code}: {e.reason}", "detail": detail}, 502)
            except Exception as e:
                self.send_json({"error": f"Beklenmeyen hata: {e}"}, 500)
            return

        self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Xray paneli: {url}  (Jira: {BASE})")
    print("Kapatmak icin bu pencerede Ctrl+C.")
    if not os.environ.get("XRAY_NO_BROWSER"):
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nKapatildi.")

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

import http.client
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
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

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

# Sorgu sonuclari da diske yazilir: sunucu kapanip acilsa bile son gorunum
# aninda geri gelir; eskiyse arayuz arka planda guncel veriyi ceker.
RESULTS_FILE = ROOT / "results_cache.json"
RESULTS_KEEP = 20


def load_results_cache() -> None:
    global _cache
    try:
        raw = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        _cache = {k: (v["ts"], v["data"]) for k, v in raw.get("results", {}).items()}
        print(f"Sonuç önbelleği yüklendi: {len(_cache)} sorgu ({RESULTS_FILE})")
    except Exception:
        _cache = {}


def save_results_cache() -> None:
    with _cache_lock:
        items = sorted(_cache.items(), key=lambda kv: -kv[1][0])[:RESULTS_KEEP]
    tmp = RESULTS_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(
            {"results": {k: {"ts": ts, "data": d} for k, (ts, d) in items}},
            ensure_ascii=False), encoding="utf-8")
        tmp.replace(RESULTS_FILE)
    except Exception:
        pass

# Kalici execution onbellegi: her execution'in tum kosum verisi diskte tutulur;
# yeniden tarama yalnizca issue "updated" damgasi degisen veya hala aktif
# (TODO'su olan) execution'lar icin yapilir.
CACHE_FILE = ROOT / "exec_cache.json"
_exec_cache: dict[str, dict] = {}
_team_cache: dict[str, list] = {}  # hedef (plan/proje) -> o hedefte simdiye dek gorulen TUM kosucular
_exec_cache_lock = threading.Lock()


CACHE_VERSION = 2  # v2: kosum kayitlarinda saat bilgisi de var


def load_exec_cache() -> None:
    global _exec_cache, _team_cache
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if data.get("version") == CACHE_VERSION:
            _exec_cache = data.get("executions", {})
            _team_cache = data.get("teams", {})
            print(f"Execution önbelleği yüklendi: {len(_exec_cache)} kayıt ({CACHE_FILE})")
        else:
            _exec_cache = {}
            _team_cache = {}
            print("Execution önbelleği eski formatta — yeniden oluşturulacak.")
    except Exception:
        _exec_cache = {}
        _team_cache = {}


def save_exec_cache() -> None:
    with _exec_cache_lock:
        entries = dict(_exec_cache)
        teams = {k: list(v) for k, v in _team_cache.items()}
    if len(entries) > 2000:  # en eskileri at
        keep = sorted(entries, key=lambda k: entries[k].get("fetchedAt", 0), reverse=True)[:2000]
        entries = {k: entries[k] for k in keep}
    tmp = CACHE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "executions": entries,
                                   "teams": teams}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception:
        pass


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
        except (OSError, http.client.HTTPException, json.JSONDecodeError):
            # ag kopmasi / yarim yanit / bozuk govde: bekleyip yeniden dene
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt + random.uniform(0, 0.5))
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
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%d/%b/%y %I:%M %p", "%d/%b/%Y %I:%M %p", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.astimezone() if dt.tzinfo else dt
        except ValueError:
            continue
    return None


def pack(counter: Counter) -> dict:
    """Bir statü sayacını UI/PPTX'in beklediği alanlara açar (statü adları Jira'daki gibi)."""
    total = sum(counter.values())
    p, f = counter.get("PASS", 0), counter.get("FAIL", 0)
    return {"total": total, "pass": p, "fail": f, "other": total - p - f,
            "statuses": dict(counter)}


def fetch_execution_full(exec_key: str, token: str) -> dict:
    """Bir execution'in TUM kosum verisi (tarih filtresi yok) — onbelleklenebilir.
    runs: [gun_iso, userKey, status]; stats: statu sayaclari; runners: kosanlar."""
    stats: Counter = Counter()
    runners: set = set()
    runs: list[list] = []
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
            st = str(t.get("status") or "").upper() or "BILINMIYOR"
            stats[st] += 1
            by = t.get("executedBy")
            if by:
                runners.add(str(by))
            # BLOCKED kosumlarda genellikle bitis tarihi olmaz; baslangic tarihiyle say
            ts = t.get("finishedOn") or (t.get("startedOn") if st == "BLOCKED" else None)
            if ts and by:
                dt = parse_dt(ts)
                if dt:
                    runs.append([dt.date().isoformat(), dt.hour, str(by), str(t.get("status") or "")])
        if len(tests) < PAGE_LIMIT:
            break
        page += 1
    return {"stats": dict(stats), "runners": sorted(runners), "runs": runs,
            "fetchedAt": time.time()}


def fetch_updated_map(exec_keys: list[str], token: str) -> dict[str, str]:
    """Execution'larin issue 'updated' damgalari — tek toplu JQL (100'luk paketler)."""
    out: dict[str, str] = {}
    for i in range(0, len(exec_keys), 100):
        chunk = exec_keys[i:i + 100]
        try:
            r = jira_get("/rest/api/2/search", token, {
                "jql": "key in (%s)" % ",".join(chunk),
                "fields": "updated", "maxResults": len(chunk),
            })
            for issue in r.get("issues", []):
                out[issue["key"]] = issue.get("fields", {}).get("updated") or ""
        except Exception:
            continue  # damga alinamayan execution "degismis" sayilir
    return out


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


def fetch_bugs(user_keys: list[str], d_from: date, d_to: date, token: str) -> list[tuple]:
    """Verilen kullanicilarin aralikta actigi Bug kayitlari: (gun_iso, userKey)."""
    if not user_keys:
        return []
    reporters = ",".join(f'"{u}"' for u in user_keys)
    jql = (f"issuetype = Bug AND reporter in ({reporters}) "
           f'AND created >= "{d_from.isoformat()}" '
           f'AND created < "{(d_to + timedelta(days=1)).isoformat()}"')
    out: list[tuple] = []
    start = 0
    while True:
        r = jira_get("/rest/api/2/search", token, {
            "jql": jql, "fields": "created,reporter", "maxResults": 100, "startAt": start,
        })
        issues = r.get("issues", [])
        for i in issues:
            f = i.get("fields", {})
            created = parse_dt(f.get("created"))
            rep = (f.get("reporter") or {}).get("name") or (f.get("reporter") or {}).get("key")
            if created and rep:
                out.append((created.date().isoformat(), str(rep)))
        start += len(issues)
        if not issues or start >= r.get("total", 0):
            break
    return out


def fetch_bug_work(user_keys: list[str], d_from: date, d_to: date, token: str) -> dict[str, dict]:
    """QA'nin bug retest yuku: uzerindeki acik bug'lar (assignee bazli, Jira is akisi
    statuleriyle) + aralikta statusunu degistirdigi bug sayisi (changelog)."""
    people: dict[str, dict] = {}
    if not user_keys:
        return people
    assignees = ",".join(f'"{u}"' for u in user_keys)
    jql = f"issuetype = Bug AND assignee in ({assignees}) AND resolution is EMPTY"
    start = 0
    while True:
        r = jira_get("/rest/api/2/search", token, {
            "jql": jql, "fields": "assignee,status", "maxResults": 100, "startAt": start,
        })
        issues = r.get("issues", [])
        for i in issues:
            f = i.get("fields", {})
            a = (f.get("assignee") or {}).get("name") or (f.get("assignee") or {}).get("key")
            st = (f.get("status") or {}).get("name") or "?"
            if a:
                p = people.setdefault(str(a), {"openStatuses": Counter(), "changed": 0})
                p["openStatuses"][st] += 1
        start += len(issues)
        if not issues or start >= r.get("total", 0):
            break
    until = (d_to + timedelta(days=1)).isoformat()
    for u in user_keys:
        try:
            cnt: Counter = Counter()
            start = 0
            while True:
                r = jira_get("/rest/api/2/search", token, {
                    "jql": (f'issuetype = Bug AND status CHANGED BY "{u}" '
                            f'DURING ("{d_from.isoformat()}","{until}")'),
                    "fields": "status", "maxResults": 100, "startAt": start,
                })
                issues = r.get("issues", [])
                for i in issues:
                    st = (i.get("fields", {}).get("status") or {}).get("name") or "?"
                    cnt[st] += 1
                start += len(issues)
                if not issues or start >= r.get("total", 0):
                    break
            if cnt:
                p = people.setdefault(u, {"openStatuses": Counter(), "changed": 0})
                p["changed"] = sum(cnt.values())
                p["changedStatuses"] = cnt
        except Exception:
            continue
    return people


PROJECT_EXEC_CAP = 300


def fetch_project_executions(project: str, token: str, cap: int = PROJECT_EXEC_CAP) -> tuple[list, int]:
    """Projedeki tum Test Execution'lar (en yeniden eskiye), cap ile sinirli.
    Donus: ([(key, summary), ...], toplam_bulunan)."""
    execs: list[tuple] = []
    total = 0
    start = 0
    while True:
        r = jira_get("/rest/api/2/search", token, {
            "jql": f'project = "{project}" AND issuetype = "Test Execution" ORDER BY created DESC',
            "fields": "summary", "maxResults": 100, "startAt": start,
        })
        total = r.get("total", 0)
        issues = r.get("issues", [])
        for i in issues:
            execs.append((i["key"], i["fields"].get("summary") or ""))
        start += len(issues)
        if not issues or start >= min(total, cap):
            break
    return execs[:cap], total


TERMINAL_STATUSES = {"KAPALI", "CLOSED", "DONE", "RESOLVED", "ÇÖZÜLDÜ", "COZULDU"}
BUGTIME_CAP = 500


def fetch_bug_status_times(projects: list[str], token: str) -> dict:
    """Projede acilmis TUM Bug'larin changelog'undan her statude gecen sureyi hesaplar
    (en yeniden eskiye, BUGTIME_CAP siniriyla). Guncel statu 'şimdi'ye kadar sayilir;
    kapali/cozuldu gibi son statulerde sayac durur."""
    proj = ",".join(f'"{p}"' for p in projects)
    jql = f"project in ({proj}) AND issuetype = Bug ORDER BY created DESC"
    issues: list[dict] = []
    start = 0
    total_found = 0
    while True:
        r = jira_get("/rest/api/2/search", token, {
            "jql": jql, "expand": "changelog", "fields": "created,status,summary",
            "maxResults": 50, "startAt": start,
        })
        total_found = r.get("total", 0)
        batch = r.get("issues", [])
        issues.extend(batch)
        start += len(batch)
        if not batch or start >= min(total_found, BUGTIME_CAP):
            break

    now = datetime.now().astimezone()
    tot: Counter = Counter()
    cnt: Counter = Counter()
    slowest: list[dict] = []
    for i in issues:
        f = i.get("fields", {})
        created = parse_dt(f.get("created"))
        if not created:
            continue
        if not created.tzinfo:
            created = created.astimezone()
        transitions = []
        for h in i.get("changelog", {}).get("histories", []):
            ht = parse_dt(h.get("created"))
            if not ht:
                continue
            for it in h.get("items", []):
                if it.get("field") == "status":
                    transitions.append((ht, it.get("fromString") or "?", it.get("toString") or "?"))
        transitions.sort(key=lambda x: x[0])
        cur_status = (f.get("status") or {}).get("name") or "?"
        per_bug: Counter = Counter()
        t_prev = created
        st_prev = transitions[0][1] if transitions else cur_status
        for ht, _frm, to_ in transitions:
            per_bug[st_prev] += max(0.0, (ht - t_prev).total_seconds())
            t_prev, st_prev = ht, to_
        if st_prev.upper() not in TERMINAL_STATUSES:
            per_bug[st_prev] += max(0.0, (now - t_prev).total_seconds())
        for s, sec in per_bug.items():
            tot[s] += sec
            cnt[s] += 1
        slowest.append({
            "key": i["key"], "summary": (f.get("summary") or "")[:90],
            "status": cur_status, "hours": sum(per_bug.values()) / 3600,
            "open": cur_status.upper() not in TERMINAL_STATUSES,
        })
    slowest.sort(key=lambda x: -x["hours"])
    statuses = sorted(
        ({"name": s, "totalHours": tot[s] / 3600,
          "avgHours": tot[s] / 3600 / cnt[s], "bugs": cnt[s]} for s in tot),
        key=lambda x: -x["avgHours"],
    )
    return {
        "count": len(issues), "found": total_found, "capped": total_found > BUGTIME_CAP,
        "statuses": statuses, "slowest": slowest[:6],
    }


def fetch_open_bugs(projects: list[str], token: str) -> dict:
    """Projedeki cozulmemis (resolution is EMPTY) tum Bug'lar: liste + dagilimlar."""
    proj = ",".join(f'"{p}"' for p in projects)
    jql = f"project in ({proj}) AND issuetype = Bug AND resolution is EMPTY ORDER BY created ASC"
    issues, start = [], 0
    while True:
        r = jira_get("/rest/api/2/search", token, {
            "jql": jql, "fields": "summary,status,assignee,created,priority",
            "maxResults": 100, "startAt": start,
        })
        batch = r.get("issues", [])
        issues.extend(batch)
        start += len(batch)
        if not batch or start >= min(r.get("total", 0), 1000):
            break

    now = datetime.now().astimezone()
    bugs = []
    st_c: Counter = Counter()
    as_c: Counter = Counter()
    pr_c: Counter = Counter()
    ages = {"90+": 0, "30-90": 0, "7-30": 0, "<7": 0}
    for i in issues:
        f = i.get("fields", {})
        created = parse_dt(f.get("created"))
        age = (now - created).days if created else 0
        assignee = ((f.get("assignee") or {}).get("displayName") or "ATANMAMIŞ")
        assignee = assignee.split(" - ")[0].split(" (")[0].strip()
        status = (f.get("status") or {}).get("name") or "?"
        prio = (f.get("priority") or {}).get("name") or "-"
        st_c[status] += 1
        as_c[assignee] += 1
        pr_c[prio] += 1
        ages["90+" if age >= 90 else "30-90" if age >= 30 else "7-30" if age >= 7 else "<7"] += 1
        bugs.append({"key": i["key"], "summary": (f.get("summary") or "")[:110],
                     "status": status, "assignee": assignee, "priority": prio,
                     "ageDays": age,
                     "created": created.strftime("%d.%m.%Y") if created else ""})
    bugs.sort(key=lambda b: -b["ageDays"])
    return {
        "count": len(bugs),
        "statuses": [{"name": s, "count": c} for s, c in st_c.most_common()],
        "assignees": [{"name": a, "count": c} for a, c in as_c.most_common(15)],
        "priorities": dict(pr_c.most_common()),
        "ages": ages,
        "list": bugs,
    }


def resolve_user(user_key: str, token: str) -> tuple[str, str, bool]:
    """Kullanici adi + hesabin aktif olup olmadigi (pasif/eski hesaplar elenebilsin)."""
    for param in ("username", "key"):
        try:
            u = jira_get("/rest/api/2/user", token, {param: user_key})
            if u.get("displayName"):
                return user_key, u["displayName"], bool(u.get("active", True))
        except Exception:
            continue
    return user_key, user_key, True


def collect(plans: list[str], d_from: date, d_to: date, token: str, force: bool = False) -> dict:
    exec_keys: list[str] = []
    exec_plan: dict[str, str] = {}
    summaries: dict[str, str] = {}
    notes: list[str] = []
    plan_keys: list[str] = []
    for target in plans:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*-\d+", target):
            # test plan key'i: planin execution listesi
            plan_keys.append(target)
            executions = jira_get(f"/rest/raven/1.0/api/testplan/{target}/testexecution", token)
            for e in executions:
                k = e.get("key")
                if k and k not in exec_plan:
                    exec_plan[k] = target
                    summaries[k] = e.get("summary") or ""
                    exec_keys.append(k)
        else:
            # proje key'i: projedeki TUM Test Execution'lar tek tek taranir
            project = target.upper()
            execs, total_found = fetch_project_executions(project, token)
            for k, summ in execs:
                if k not in exec_plan:
                    exec_plan[k] = project
                    summaries[k] = summ
                    exec_keys.append(k)
            if total_found > len(execs):
                notes.append(f"{project}: {total_found} execution'dan en yeni {len(execs)} tanesi tarandı")

    # changelog kontrolu: tum execution'larin updated damgasi tek toplu sorguyla
    updated_map = fetch_updated_map(exec_keys, token)

    def get_exec(key: str) -> tuple[str, dict, bool]:
        upd = updated_map.get(key)
        with _exec_cache_lock:
            ent = _exec_cache.get(key)
        # Not: kosum statusu degisiklikleri issue 'updated' damgasini her zaman
        # degistirmez; bu yuzden tamamlanmis kayitlar bile en gec 24 saatte bir
        # yeniden taranir (BLOCKED/PASS kaymalarini yakalamak icin).
        fresh = (ent and not force and upd and ent.get("updated") == upd
                 and ent.get("stats", {}).get("TODO", 0) == 0
                 and time.time() - ent.get("fetchedAt", 0) < 86400)
        if fresh:
            return key, ent, True
        ent = fetch_execution_full(key, token)
        ent["updated"] = upd
        with _exec_cache_lock:
            _exec_cache[key] = ent
        return key, ent, False

    runs: list[tuple] = []
    exec_list: list[dict] = []
    person_exec: dict[str, dict] = {}
    person_hour: dict[str, Counter] = {}
    cache_hits = 0
    f_iso, t_iso = d_from.isoformat(), d_to.isoformat()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fut_map = {pool.submit(get_exec, k): k for k in exec_keys}
        all_runners: set = set()
        for fut in as_completed(fut_map):
            key, ent, hit = fut.result()
            cache_hits += hit
            stats = Counter(ent["stats"])
            all_runners |= set(ent["runners"])
            for day_iso, hour, user, status in ent["runs"]:
                if f_iso <= day_iso <= t_iso:
                    runs.append((day_iso, user, status))
                    st = (status or "").upper() or "BILINMIYOR"
                    person_exec.setdefault(user, {}).setdefault(key, Counter())[st] += 1
                    person_hour.setdefault(user, Counter())[hour] += 1
            total = sum(stats.values())
            todo = stats.get("TODO", 0)
            exec_list.append({
                "key": key, "plan": exec_plan.get(key, ""), "summary": summaries.get(key, ""),
                "total": total, "todo": todo, "done": total - todo,
                "pass": stats.get("PASS", 0), "fail": stats.get("FAIL", 0),
                "other": total - todo - stats.get("PASS", 0) - stats.get("FAIL", 0),
                "statuses": dict(stats),
            })

    # kalici ekip kaydi: bu hedeflerde simdiye dek gorulen TUM kosucular —
    # 300'luk proje siniri disinda kalan ya da uzerine yeniden kosum yapilan
    # execution'larin kosuculari da boylece kacmaz
    with _exec_cache_lock:
        for target in plans:
            all_runners |= set(_team_cache.get(target, []))
        for target in plans:
            _team_cache[target] = sorted(set(_team_cache.get(target, [])) | all_runners)
    save_exec_cache()
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

    per_plan: list[dict] = []
    plan_total = plan_done = 0
    for plan in plan_keys:
        t, d = fetch_plan_progress(plan, token)
        per_plan.append({"plan": plan, "total": t, "done": d, "remaining": max(t - d, 0)})
        plan_total += t
        plan_done += d

    per_person: dict[str, Counter] = {}
    per_day: dict[str, Counter] = {}
    person_day: dict[str, dict] = {}
    status_totals: Counter = Counter()
    for day_iso, user, status in runs:
        st = (status or "").upper() or "BILINMIYOR"
        status_totals[st] += 1
        per_person.setdefault(user, Counter())[st] += 1
        per_day.setdefault(day_iso, Counter())[st] += 1
        person_day.setdefault(user, {}).setdefault(day_iso, Counter())[st] += 1

    # ekip (execution'larda kosum yapmis herkes) araliktaki Bug kayitlari — reporter bazli
    bug_person_day: dict[str, Counter] = {}
    bug_day: Counter = Counter()
    bug_person: Counter = Counter()
    try:
        for b_day, b_user in fetch_bugs(sorted(all_runners | set(per_person.keys())), d_from, d_to, token):
            bug_person[b_user] += 1
            bug_day[b_day] += 1
            bug_person_day.setdefault(b_user, Counter())[b_day] += 1
        bugs_error = None
    except Exception as e:
        bugs_error = str(e)[:200]

    try:
        bug_work = fetch_bug_work(sorted(all_runners | set(per_person.keys())), d_from, d_to, token)
        bug_work_error = None
    except Exception as e:
        bug_work = {}
        bug_work_error = str(e)[:200]

    names: dict[str, str] = {}
    user_active: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(resolve_user, k, token)
                   for k in set(per_person) | set(bug_person) | set(bug_work)]
        for fut in as_completed(futures):
            key, name, active = fut.result()
            names[key] = name
            user_active[key] = active

    people = sorted(
        ({"key": k, "name": names.get(k, k), **pack(c)} for k, c in per_person.items()),
        key=lambda x: (-x["total"], x["name"].lower()),
    )

    day_range: list[str] = []
    cur = d_from
    while cur <= d_to:
        day_range.append(cur.isoformat())
        cur += timedelta(days=1)
    days = [{"date": iso, **pack(per_day.get(iso, Counter()))} for iso in day_range]

    person_days = [{
        "key": p["key"],
        "name": p["name"],
        "days": [{"date": iso, **pack(person_day.get(p["key"], {}).get(iso, Counter()))}
                 for iso in day_range],
    } for p in people]

    person_hours = [{
        "key": p["key"], "name": p["name"], "total": p["total"],
        "hours": [person_hour.get(p["key"], Counter()).get(h, 0) for h in range(24)],
    } for p in people]
    hour_totals = [sum(ph["hours"][h] for ph in person_hours) for h in range(24)]

    person_execs = [{
        "key": p["key"], "name": p["name"], "total": p["total"],
        "execs": sorted(
            ({"key": ek, "plan": exec_plan.get(ek, ""), "summary": summaries.get(ek, ""),
              **pack(cnt)}
             for ek, cnt in person_exec.get(p["key"], {}).items()),
            key=lambda x: -x["total"],
        ),
    } for p in people]

    # pasif (kapatilmis/eski) Jira hesaplari retest takibine dahil edilmez
    bug_work_people = sorted(
        ({
            "key": k, "name": names.get(k, k),
            "open": sum(v["openStatuses"].values()),
            "openStatuses": dict(v["openStatuses"]),
            "changed": v["changed"],
            "changedStatuses": dict(v.get("changedStatuses", {})),
        } for k, v in bug_work.items() if user_active.get(k, True)),
        key=lambda x: (-x["open"], -x["changed"], x["name"].lower()),
    )

    bugs = {
        "total": sum(bug_person.values()),
        "error": bugs_error,
        "perDay": [{"date": iso, "total": bug_day.get(iso, 0)} for iso in day_range],
        "personDays": sorted(
            ({
                "key": k, "name": names.get(k, k), "total": bug_person[k],
                "days": [{"date": iso, "total": bug_person_day.get(k, Counter()).get(iso, 0)}
                         for iso in day_range],
            } for k in bug_person),
            key=lambda x: (-x["total"], x["name"].lower()),
        ),
    }

    total = len(runs)
    n_days = (d_to - d_from).days + 1
    rate = total / n_days
    remaining = max(plan_total - plan_done, 0)
    estimate = math.ceil(remaining / rate) if rate > 0 and remaining > 0 else None

    return {
        "plan": ", ".join(plans),
        "from": d_from.isoformat(),
        "to": d_to.isoformat(),
        "executions": len(exec_keys),
        "total": total,
        "passCount": sum(p["pass"] for p in people),
        "failCount": sum(p["fail"] for p in people),
        "otherCount": sum(p["other"] for p in people),
        "people": people,
        "days": days,
        "personDays": person_days,
        "personExecs": person_execs,
        "personHours": person_hours,
        "hourTotals": hour_totals,
        "statusList": [s for s, _ in status_totals.most_common()],
        "statusTotals": dict(status_totals),
        "bugs": bugs,
        "bugWork": {"error": bug_work_error, "people": bug_work_people},
        "notes": notes,
        "teamSize": len(all_runners | set(per_person.keys())),
        "execCache": {"hits": cache_hits, "scanned": len(exec_keys) - cache_hits},
        "execProgress": exec_list,
        "progress": {
            "total": plan_total,
            "done": plan_done,
            "remaining": remaining,
            "estimateDays": estimate,
            "perPlan": per_plan,
        },
        "generatedAt": datetime.now().strftime("%H:%M"),
    }


def demo_data(d_from: date, d_to: date) -> dict:
    people_names = [
        "Ayşe Yılmaz", "Mehmet Demir", "Zeynep Kaya",
        "Emre Şahin", "Elif Çelik", "Burak Arslan",
    ]
    demo_statuses = ["PASS", "FAIL", "EXECUTING", "BLOCKED", "ABORTED"]
    weights = [0.68, 0.14, 0.08, 0.06, 0.04]
    rng = random.Random(f"{d_from}{d_to}")
    per_person: dict[str, Counter] = {}
    per_day: dict[str, Counter] = {}
    person_day: dict[str, dict] = {}
    status_totals: Counter = Counter()
    day_range: list[str] = []
    cur = d_from
    while cur <= d_to:
        iso = cur.isoformat()
        day_range.append(iso)
        weekend = cur.weekday() >= 5
        for name in people_names:
            for _ in range(0 if weekend else rng.randint(0, 14)):
                st = rng.choices(demo_statuses, weights)[0]
                status_totals[st] += 1
                per_person.setdefault(name, Counter())[st] += 1
                per_day.setdefault(iso, Counter())[st] += 1
                person_day.setdefault(name, {}).setdefault(iso, Counter())[st] += 1
        cur += timedelta(days=1)
    people = sorted(
        ({"key": n, "name": n, **pack(c)} for n, c in per_person.items()),
        key=lambda x: (-x["total"], x["name"]),
    )
    days = [{"date": iso, **pack(per_day.get(iso, Counter()))} for iso in day_range]
    person_days = [{
        "key": p["key"], "name": p["name"],
        "days": [{"date": iso, **pack(person_day.get(p["key"], {}).get(iso, Counter()))}
                 for iso in day_range],
    } for p in people]
    total = sum(p["total"] for p in people)
    done, plan_total = 128, 196
    rate = total / len(day_range)
    bug_pdays = []
    for p in people:
        ds = [{"date": iso, "total": (rng.randint(1, 2) if rng.random() < 0.35 else 0)}
              for iso in day_range]
        bug_pdays.append({"key": p["key"], "name": p["name"],
                          "total": sum(d["total"] for d in ds), "days": ds})
    bug_pdays.sort(key=lambda x: (-x["total"], x["name"]))
    bugs_demo = {
        "total": sum(p["total"] for p in bug_pdays),
        "error": None,
        "perDay": [{"date": iso, "total": sum(p["days"][i]["total"] for p in bug_pdays)}
                   for i, iso in enumerate(day_range)],
        "personDays": bug_pdays,
    }
    exec_list = []
    for i in range(1, 8):
        t = rng.randint(8, 60)
        c: Counter = Counter()
        for _ in range(t):
            r = rng.random()
            c["PASS" if r < 0.55 else "FAIL" if r < 0.7 else "TODO" if r < 0.95 else "ABORTED"] += 1
        todo = c.get("TODO", 0)
        exec_list.append({
            "key": f"TKP3576-{29900 + i}", "plan": "TKP3576-29946",
            "summary": f"Demo Execution {i} — Regresyon Paketi",
            "total": t, "todo": todo, "done": t - todo,
            "pass": c.get("PASS", 0), "fail": c.get("FAIL", 0),
            "other": t - todo - c.get("PASS", 0) - c.get("FAIL", 0),
            "statuses": dict(c),
        })
    exec_list.sort(key=lambda e: e["done"] / e["total"] if e["total"] else 1.0)
    person_execs = []
    for p in people:
        remaining = Counter(p["statuses"])
        keys = rng.sample([e["key"] for e in exec_list], min(rng.randint(2, 3), len(exec_list)))
        p_execs = []
        for i, ek in enumerate(keys):
            cnt: Counter = Counter()
            for s in list(remaining):
                take = remaining[s] if i == len(keys) - 1 else rng.randint(0, remaining[s])
                if take:
                    cnt[s] += take
                    remaining[s] -= take
            if sum(cnt.values()):
                p_execs.append({"key": ek, "plan": "TKP3576-29946",
                                "summary": "Demo Execution — Regresyon Paketi", **pack(cnt)})
        p_execs.sort(key=lambda x: -x["total"])
        person_execs.append({"key": p["key"], "name": p["name"], "total": p["total"], "execs": p_execs})
    person_hours = []
    for p in people:
        hours = [0] * 24
        for _ in range(p["total"]):
            h = min(23, max(0, int(rng.gauss(13, 2.5))))
            hours[h] += 1
        person_hours.append({"key": p["key"], "name": p["name"], "total": p["total"], "hours": hours})
    hour_totals = [sum(ph["hours"][h] for ph in person_hours) for h in range(24)]
    return {
        "plan": "TKP3576-29946", "from": d_from.isoformat(), "to": d_to.isoformat(),
        "executions": 14, "total": total, "execProgress": exec_list,
        "passCount": sum(p["pass"] for p in people),
        "failCount": sum(p["fail"] for p in people),
        "otherCount": sum(p["other"] for p in people),
        "people": people, "days": days, "personDays": person_days,
        "personExecs": person_execs,
        "personHours": person_hours, "hourTotals": hour_totals,
        "statusList": [s for s, _ in status_totals.most_common()],
        "statusTotals": dict(status_totals),
        "bugs": bugs_demo,
        "bugWork": {"error": None, "people": [
            {"key": p["key"], "name": p["name"],
             "open": (o := rng.randint(0, 6)),
             "openStatuses": ({"Retest": o - o // 3, "Open": o // 3} if o else {}),
             "changed": (c := rng.randint(0, 9)),
             "changedStatuses": ({"Kapalı": c - c // 4, "Yeniden açıldı": c // 4} if c else {})}
            for p in people
        ]},
        "progress": {
            "total": plan_total, "done": done, "remaining": plan_total - done,
            "estimateDays": math.ceil((plan_total - done) / rate) if rate else None,
            "perPlan": [{"plan": "TKP3576-29946", "total": plan_total, "done": done,
                         "remaining": plan_total - done}],
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
        plan_slug = re.sub(r"[^\w.-]+", "_", data["plan"])
        fname = f"xray-{plan_slug}-{data['from']}_{data['to']}.pptx"
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.presentationml.presentation")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.log_message("istemci baglantiyi kapatti: %s", self.path)

    def send_json(self, obj, status=200):
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # istemci beklemeden vazgecmis; sessizce gec
            self.log_message("istemci baglantiyi kapatti: %s", self.path)

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

        if parsed.path == "/api/bugtime":
            if qs.get("demo", [""])[0]:
                rng = random.Random("bt-demo")
                sts = [("Open", 18), ("Development", 60), ("Ready to Test", 30),
                       ("Retest", 12), ("On Hold", 90)]
                self.send_json({"count": 24, "found": 24, "capped": False, "statuses": [
                    {"name": n, "avgHours": h + rng.random() * 20,
                     "totalHours": (h + rng.random() * 20) * rng.randint(5, 20),
                     "bugs": rng.randint(5, 24)} for n, h in sts
                ], "slowest": [
                    {"key": f"TKP3576-4{i}9", "summary": f"Demo bug {i}", "status": "Development",
                     "hours": 200 - i * 25, "open": True} for i in range(1, 5)
                ]})
                return
            token = get_token()
            if not token:
                self.send_json({"error": "token yok"}, 401)
                return
            targets = [p.upper() for p in re.split(r"[,;\s]+", qs.get("plan", [""])[0].strip()) if p]
            projects = sorted({t.split("-")[0] for t in targets if t})
            if not projects:
                self.send_json({"error": "Plan/proje gerekli."}, 400)
                return
            bt_key = "bt|" + ",".join(projects) + "|all"
            with _cache_lock:
                hit = _cache.get(bt_key)
            if hit and time.time() - hit[0] < CACHE_TTL:
                self.send_json(hit[1])
                return
            try:
                data = fetch_bug_status_times(projects, token)
                with _cache_lock:
                    _cache[bt_key] = (time.time(), data)
                save_results_cache()
                self.send_json(data)
            except urllib.error.HTTPError as e:
                self.send_json({"error": f"Jira HTTP {e.code}: {e.reason}"}, 502)
            except Exception as e:
                self.send_json({"error": f"Beklenmeyen hata: {e}"}, 500)
            return

        if parsed.path == "/api/openbugs":
            if qs.get("demo", [""])[0]:
                rng = random.Random("ob-demo")
                self.send_json({"count": 34, "statuses": [
                    {"name": "Open", "count": 14}, {"name": "On Hold", "count": 9},
                    {"name": "Ready to Test", "count": 7}, {"name": "Development", "count": 4}],
                    "assignees": [{"name": n, "count": rng.randint(2, 9)} for n in
                                  ("Ayşe Yılmaz", "Mehmet Demir", "Zeynep Kaya", "ATANMAMIŞ")],
                    "priorities": {"Medium": 30, "High": 3, "Kritik": 1},
                    "ages": {"90+": 6, "30-90": 8, "7-30": 12, "<7": 8},
                    "list": [{"key": f"TKP3576-{100+i}", "summary": f"Demo açık bug {i}",
                              "status": "On Hold", "assignee": "Ayşe Yılmaz", "priority": "Medium",
                              "ageDays": 200 - i * 9, "created": "01.01.2026"} for i in range(1, 16)]})
                return
            token = get_token()
            if not token:
                self.send_json({"error": "token yok"}, 401)
                return
            targets = [p.upper() for p in re.split(r"[,;\s]+", qs.get("plan", [""])[0].strip()) if p]
            projects = sorted({t.split("-")[0] for t in targets if t})
            if not projects:
                self.send_json({"error": "Plan/proje gerekli."}, 400)
                return
            ob_key = "ob|" + ",".join(projects)
            with _cache_lock:
                hit = _cache.get(ob_key)
            if hit and time.time() - hit[0] < CACHE_TTL:
                self.send_json(hit[1])
                return
            try:
                data = fetch_open_bugs(projects, token)
                with _cache_lock:
                    _cache[ob_key] = (time.time(), data)
                save_results_cache()
                self.send_json(data)
            except urllib.error.HTTPError as e:
                self.send_json({"error": f"Jira HTTP {e.code}: {e.reason}"}, 502)
            except Exception as e:
                self.send_json({"error": f"Beklenmeyen hata: {e}"}, 500)
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
            plans = [p.upper() for p in re.split(r"[,;\s]+", qs.get("plan", [""])[0].strip()) if p]
            if not plans:
                self.send_json({"error": "Test plan key'i gerekli."}, 400)
                return

            cache_key = "|".join(sorted(plans)) + "|" + d_from.isoformat() + "|" + d_to.isoformat()
            refresh = bool(qs.get("refresh", [""])[0])
            full = bool(qs.get("full", [""])[0])
            if not refresh and not full:
                with _cache_lock:
                    hit = _cache.get(cache_key)
                if hit:
                    data = dict(hit[1])
                    data["cached"] = True
                    if time.time() - hit[0] >= CACHE_TTL:
                        # eski kayit: yine de aninda goster; arayuz arka planda tazeler
                        data["stale"] = True
                        data["cachedAt"] = datetime.fromtimestamp(hit[0]).strftime("%d.%m.%Y %H:%M")
                    self.send_result(data, want_pptx)
                    return
            try:
                data = collect(plans, d_from, d_to, token, force=full)
                with _cache_lock:
                    _cache[cache_key] = (time.time(), data)
                save_results_cache()
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
    load_exec_cache()
    load_results_cache()
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

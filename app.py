# -*- coding: utf-8 -*-
"""
Xray Test Otomasyon Aracı
-------------------------
20 satıra kadar test senaryosu girilen web arayüzü. Girilen senaryoları
Jira/Xray API'si ile:
  1. Test issue'su olarak oluşturur (manuel adımlarıyla birlikte)
  2. Bir Test Set'e bağlar (yeni oluşturur veya mevcut key kullanır)
  3. Test Execution oluşturur ve tüm testleri PASS olarak işaretler

Kimlik doğrulama: Jira Personal Access Token (PAT, Bearer). PAT ilk
açılışta arayüzde sorulur ve config.json dosyasına yerel olarak kaydedilir.
"""

import json
import os
import stat

import requests
from flask import Flask, jsonify, render_template, request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "jiraUrl": "",
    "pat": "",
    "projectKey": "",
    "testIssueType": "Test",
    "testSetIssueType": "Test Set",
    "testExecIssueType": "Test Execution",
    "verifySsl": True,
}

app = Flask(__name__)


# ----------------------------------------------------------------------------
# Konfigürasyon
# ----------------------------------------------------------------------------

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(json.load(f))
        return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    # PAT içerdiği için dosyayı yalnızca sahibi okuyabilsin
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)


def is_configured(cfg):
    return bool(cfg.get("jiraUrl") and cfg.get("pat") and cfg.get("projectKey"))


# ----------------------------------------------------------------------------
# Jira / Xray HTTP yardımcıları
# ----------------------------------------------------------------------------

class JiraError(Exception):
    pass


def jira_request(cfg, method, path, json_body=None, params=None):
    url = cfg["jiraUrl"].rstrip("/") + path
    headers = {
        "Authorization": "Bearer " + cfg["pat"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        resp = requests.request(
            method,
            url,
            headers=headers,
            json=json_body,
            params=params,
            timeout=60,
            verify=cfg.get("verifySsl", True),
        )
    except requests.exceptions.SSLError as e:
        raise JiraError("SSL hatası: %s (Ayarlardan SSL doğrulamayı kapatabilirsiniz)" % e)
    except requests.exceptions.RequestException as e:
        raise JiraError("Bağlantı hatası: %s" % e)

    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                msgs = body.get("errorMessages") or []
                errs = body.get("errors") or {}
                parts = list(msgs) + ["%s: %s" % (k, v) for k, v in errs.items()]
                detail = "; ".join(str(p) for p in parts) or json.dumps(body, ensure_ascii=False)[:300]
            else:
                detail = str(body)[:300]
        except ValueError:
            detail = (resp.text or "")[:300]
        raise JiraError("HTTP %s (%s %s): %s" % (resp.status_code, method, path, detail))

    if resp.status_code == 204 or not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


def create_issue(cfg, issue_type, summary, description=""):
    fields = {
        "project": {"key": cfg["projectKey"]},
        "summary": summary,
        "issuetype": {"name": issue_type},
    }
    if description:
        fields["description"] = description
    data = jira_request(cfg, "POST", "/rest/api/2/issue", {"fields": fields})
    return data["key"]


def add_test_steps(cfg, test_key, steps):
    """Manuel test adımlarını ekler. Önce Xray v2 API, olmazsa v1 denenir."""
    warnings = []
    for i, step in enumerate(steps, start=1):
        body_v2 = {
            "action": step.get("action", ""),
            "data": step.get("data", ""),
            "result": step.get("expected", ""),
        }
        try:
            jira_request(cfg, "POST", "/rest/raven/2.0/api/test/%s/steps" % test_key, body_v2)
            continue
        except JiraError:
            pass
        body_v1 = {
            "step": step.get("action", ""),
            "data": step.get("data", ""),
            "result": step.get("expected", ""),
        }
        try:
            jira_request(cfg, "PUT", "/rest/raven/1.0/api/test/%s/step" % test_key, body_v1)
        except JiraError as e:
            warnings.append("%s: %d. adım eklenemedi (%s)" % (test_key, i, e))
    return warnings


def add_tests_to_testset(cfg, testset_key, test_keys):
    jira_request(
        cfg,
        "POST",
        "/rest/raven/1.0/api/testset/%s/test" % testset_key,
        {"add": test_keys},
    )


def create_execution_and_pass(cfg, name, test_keys, status="PASS"):
    """Önce Xray import/execution ile tek istekte oluşturup PASS'ler;
    olmazsa issue + testexec add + testrun status yoluna düşer."""
    body = {
        "info": {"summary": name, "project": cfg["projectKey"]},
        "tests": [{"testKey": k, "status": status} for k in test_keys],
    }
    try:
        data = jira_request(cfg, "POST", "/rest/raven/1.0/import/execution", body)
        exec_key = (data.get("testExecIssue") or {}).get("key")
        if exec_key:
            return exec_key, []
    except JiraError:
        pass

    # Fallback: adım adım
    warnings = []
    exec_key = create_issue(cfg, cfg.get("testExecIssueType", "Test Execution"), name)
    jira_request(
        cfg,
        "POST",
        "/rest/raven/1.0/api/testexec/%s/test" % exec_key,
        {"add": test_keys},
    )
    for tk in test_keys:
        try:
            run = jira_request(
                cfg,
                "GET",
                "/rest/raven/1.0/api/testrun",
                params={"testExecIssueKey": exec_key, "testIssueKey": tk},
            )
            run_id = run.get("id")
            if run_id is None:
                raise JiraError("test run id bulunamadı")
            jira_request(
                cfg,
                "PUT",
                "/rest/raven/1.0/api/testrun/%s/status" % run_id,
                params={"status": status},
            )
        except JiraError as e:
            warnings.append("%s PASS yapılamadı: %s" % (tk, e))
    return exec_key, warnings


# ----------------------------------------------------------------------------
# HTTP endpoint'leri
# ----------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    cfg = load_config()
    return jsonify({
        "configured": is_configured(cfg),
        "jiraUrl": cfg.get("jiraUrl", ""),
        "projectKey": cfg.get("projectKey", ""),
        "testIssueType": cfg.get("testIssueType", "Test"),
        "testSetIssueType": cfg.get("testSetIssueType", "Test Set"),
        "testExecIssueType": cfg.get("testExecIssueType", "Test Execution"),
        "verifySsl": cfg.get("verifySsl", True),
        "hasPat": bool(cfg.get("pat")),
    })


@app.route("/api/config", methods=["POST"])
def set_config():
    data = request.get_json(force=True) or {}
    cfg = load_config()

    for key in ("jiraUrl", "projectKey", "testIssueType", "testSetIssueType", "testExecIssueType"):
        if key in data and str(data[key]).strip():
            cfg[key] = str(data[key]).strip()
    if "verifySsl" in data:
        cfg["verifySsl"] = bool(data["verifySsl"])
    if data.get("pat"):
        cfg["pat"] = str(data["pat"]).strip()

    if not is_configured(cfg):
        return jsonify({"ok": False, "error": "Jira URL, PAT ve Proje Anahtarı zorunludur."}), 400

    # Bağlantıyı doğrula
    try:
        me = jira_request(cfg, "GET", "/rest/api/2/myself")
    except JiraError as e:
        return jsonify({"ok": False, "error": "Bağlantı doğrulanamadı: %s" % e}), 400

    save_config(cfg)
    return jsonify({"ok": True, "user": me.get("displayName") or me.get("name") or ""})


@app.route("/api/tests", methods=["POST"])
def api_create_test():
    """Tek bir test senaryosu oluşturur (arayüz satır satır çağırır)."""
    cfg = load_config()
    if not is_configured(cfg):
        return jsonify({"ok": False, "error": "Önce ayarları (PAT vb.) kaydedin."}), 400

    data = request.get_json(force=True) or {}
    summary = (data.get("summary") or "").strip()
    if not summary:
        return jsonify({"ok": False, "error": "Senaryo adı boş."}), 400

    try:
        key = create_issue(cfg, cfg.get("testIssueType", "Test"), summary, data.get("description") or "")
    except JiraError as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    warnings = []
    steps = data.get("steps") or []
    if steps:
        warnings = add_test_steps(cfg, key, steps)

    return jsonify({"ok": True, "key": key, "warnings": warnings})


@app.route("/api/testset", methods=["POST"])
def api_testset():
    """Test Set oluşturur (veya mevcut key'i kullanır) ve testleri bağlar."""
    cfg = load_config()
    if not is_configured(cfg):
        return jsonify({"ok": False, "error": "Önce ayarları kaydedin."}), 400

    data = request.get_json(force=True) or {}
    test_keys = data.get("testKeys") or []
    if not test_keys:
        return jsonify({"ok": False, "error": "Bağlanacak test yok."}), 400

    existing = (data.get("existingKey") or "").strip()
    try:
        if existing:
            testset_key = existing
        else:
            name = (data.get("name") or "").strip() or "Test Set"
            testset_key = create_issue(cfg, cfg.get("testSetIssueType", "Test Set"), name)
        add_tests_to_testset(cfg, testset_key, test_keys)
    except JiraError as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    return jsonify({"ok": True, "key": testset_key})


@app.route("/api/execution", methods=["POST"])
def api_execution():
    """Test Execution oluşturur ve testleri PASS olarak işaretler."""
    cfg = load_config()
    if not is_configured(cfg):
        return jsonify({"ok": False, "error": "Önce ayarları kaydedin."}), 400

    data = request.get_json(force=True) or {}
    test_keys = data.get("testKeys") or []
    if not test_keys:
        return jsonify({"ok": False, "error": "Çalıştırılacak test yok."}), 400

    name = (data.get("name") or "").strip() or "Test Execution"
    status = (data.get("status") or "PASS").strip() or "PASS"
    try:
        exec_key, warnings = create_execution_and_pass(cfg, name, test_keys, status)
    except JiraError as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    return jsonify({"ok": True, "key": exec_key, "warnings": warnings})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

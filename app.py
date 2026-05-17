"""HR Employee Portal — Flask + Odoo XML-RPC (V19)."""
import os
import ssl
import xmlrpc.client
from functools import wraps
from datetime import datetime

from dotenv import load_dotenv
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    abort,
)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

ODOO_URL = os.environ.get("ODOO_URL", "").rstrip("/")
ODOO_DB = os.environ.get("ODOO_DB", "")
ODOO_USERNAME = os.environ.get("ODOO_USERNAME", "")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")

REQUEST_TYPES = {
    "salary_certificate": {
        "ar": "تعريف بالراتب",
        "en": "Salary Certificate",
        "icon": "📄",
        "category_keywords": ["salary certificate", "تعريف بالراتب"],
        "fields": [
            {"name": "addressed_to", "ar": "موجه إلى", "en": "Addressed To", "type": "text", "required": True},
            {"name": "purpose", "ar": "الغرض", "en": "Purpose", "type": "textarea", "required": False},
        ],
    },
    "recruitment_office": {
        "ar": "طلب مكتب الاستقدام",
        "en": "Recruitment Office Request",
        "icon": "🏢",
        "category_keywords": ["recruitment", "استقدام"],
        "fields": [
            {"name": "office_name", "ar": "اسم المكتب", "en": "Office Name", "type": "text", "required": True},
            {"name": "worker_type", "ar": "نوع العامل/العاملة", "en": "Worker Type", "type": "text", "required": True},
            {"name": "notes", "ar": "ملاحظات", "en": "Notes", "type": "textarea", "required": False},
        ],
    },
    "salary_stabilization": {
        "ar": "طلب تثبيت راتب",
        "en": "Salary Stabilization",
        "icon": "💰",
        "category_keywords": ["stabilization", "تثبيت"],
        "fields": [
            {"name": "bank_name", "ar": "اسم البنك", "en": "Bank Name", "type": "text", "required": True},
            {"name": "reason", "ar": "السبب", "en": "Reason", "type": "textarea", "required": False},
        ],
    },
    "exit_reentry_visa": {
        "ar": "تأشيرة خروج وعودة",
        "en": "Exit / Re-entry Visa",
        "icon": "✈️",
        "category_keywords": ["exit", "خروج", "visa", "تأشيرة"],
        "fields": [
            {"name": "departure_date", "ar": "تاريخ المغادرة", "en": "Departure Date", "type": "date", "required": True},
            {"name": "return_date", "ar": "تاريخ العودة", "en": "Return Date", "type": "date", "required": True},
            {"name": "destination", "ar": "الوجهة", "en": "Destination", "type": "text", "required": True},
            {"name": "visa_type", "ar": "نوع التأشيرة", "en": "Visa Type", "type": "select", "required": True,
             "options": [
                 {"value": "single", "ar": "مفردة", "en": "Single"},
                 {"value": "multiple", "ar": "متعددة", "en": "Multiple"},
             ]},
        ],
    },
    "resignation": {
        "ar": "استقالة",
        "en": "Resignation",
        "icon": "📝",
        "category_keywords": ["resignation", "استقالة"],
        "fields": [
            {"name": "last_working_day", "ar": "آخر يوم عمل", "en": "Last Working Day", "type": "date", "required": True},
            {"name": "reason", "ar": "السبب", "en": "Reason", "type": "textarea", "required": True},
        ],
    },
    "bank_letter": {
        "ar": "طلب خطاب للبنوك",
        "en": "Bank Letter Request",
        "icon": "🏦",
        "category_keywords": ["bank letter", "خطاب", "بنك"],
        "fields": [
            {"name": "bank_name", "ar": "اسم البنك", "en": "Bank Name", "type": "text", "required": True},
            {"name": "letter_purpose", "ar": "الغرض من الخطاب", "en": "Letter Purpose", "type": "textarea", "required": True},
        ],
    },
}

# Marker used to identify requests submitted by a specific employee through this portal.
# We embed it inside the description so we can later filter `approval.request` records.
EMP_TAG = "[HR-PORTAL-EMP:{emp_id}]"


# ----------------------------------------------------------------------------
# Odoo XML-RPC helpers
# ----------------------------------------------------------------------------
def _ssl_context():
    return ssl.create_default_context()


def odoo_authenticate():
    """Authenticate the admin user; returns uid or None."""
    if not (ODOO_URL and ODOO_DB and ODOO_USERNAME and ODOO_API_KEY):
        return None
    common = xmlrpc.client.ServerProxy(
        f"{ODOO_URL}/xmlrpc/2/common",
        allow_none=True,
        context=_ssl_context(),
    )
    return common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})


def odoo_execute(model, method, args, kwargs=None):
    """Run execute_kw against Odoo with the admin credentials."""
    uid = odoo_authenticate()
    if not uid:
        raise RuntimeError("Unable to authenticate against Odoo. Check .env credentials.")
    models = xmlrpc.client.ServerProxy(
        f"{ODOO_URL}/xmlrpc/2/object",
        allow_none=True,
        context=_ssl_context(),
    )
    return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, model, method, args, kwargs or {})


def get_admin_uid():
    return odoo_authenticate()


# ----------------------------------------------------------------------------
# Employee verification
# ----------------------------------------------------------------------------
def search_employees(query):
    """Search hr.employee by name (partial) or barcode (exact).

    Empty query → returns up to 10 active employees, no name/barcode filter.
    """
    query = (query or "").strip()
    fields = ["id", "name", "job_title", "department_id", "barcode", "pin"]

    if not query:
        domain = [("active", "=", True)]
    else:
        domain = [
            ("active", "=", True),
            "|",
            ("name", "ilike", query),
            ("barcode", "=", query),
        ]

    return odoo_execute(
        "hr.employee",
        "search_read",
        [domain],
        {"fields": fields, "limit": 10},
    )


def verify_employee_pin(employee_id, pin):
    """Confirm that the supplied PIN matches hr.employee.pin for this employee."""
    if not pin:
        return None
    records = odoo_execute(
        "hr.employee",
        "read",
        [[int(employee_id)], ["id", "name", "pin", "job_title", "department_id"]],
    )
    if not records:
        return None
    emp = records[0]
    stored = str(emp.get("pin") or "").strip()
    if not stored or stored != str(pin).strip():
        return None
    return emp


# ----------------------------------------------------------------------------
# Approval requests
# ----------------------------------------------------------------------------
def find_category_id(req_type_key):
    """Find an approval.category that matches the request type keywords."""
    cfg = REQUEST_TYPES.get(req_type_key)
    if not cfg:
        return None
    try:
        categories = odoo_execute(
            "approval.category",
            "search_read",
            [[]],
            {"fields": ["id", "name"], "limit": 200},
        )
    except Exception:
        return None
    for keyword in cfg["category_keywords"]:
        kw = keyword.lower()
        for cat in categories:
            if kw in (cat.get("name") or "").lower():
                return cat["id"]
    return None


def approval_request_fields():
    """Return the dict of fields available on approval.request."""
    return odoo_execute(
        "approval.request",
        "fields_get",
        [],
        {"attributes": ["string", "type", "relation"]},
    )


# Candidate field names (in priority order) for the request body / notes.
# In Odoo V19 'description' was removed from approval.request; 'reason' is
# the typical replacement, but installs vary, so probe and pick the first match.
DESCRIPTION_FIELD_CANDIDATES = ("reason", "note", "description", "request_note")

# Many2one to hr.employee on approval.request, when present, lets us link the
# request to the actual employee record (shows employee name in Odoo and lets
# us filter without a description marker).
EMPLOYEE_FIELD_CANDIDATES = ("employee_id", "x_employee_id", "hr_employee_id")


def pick_description_field(available_fields):
    for f in DESCRIPTION_FIELD_CANDIDATES:
        if f in available_fields:
            return f
    return None


def pick_employee_field(available_fields):
    for f in EMPLOYEE_FIELD_CANDIDATES:
        info = available_fields.get(f)
        if info and info.get("type") == "many2one":
            return f
    return None


def build_body(req_type_key, employee, form_data, include_marker=False):
    cfg = REQUEST_TYPES[req_type_key]
    lines = []
    if include_marker:
        lines.append(EMP_TAG.format(emp_id=employee["id"]))
    lines.extend([
        f"Request Type: {cfg['en']} ({cfg['ar']})",
        f"Employee: {employee['name']} (ID: {employee['id']})",
        "",
        "--- Details ---",
    ])
    for field in cfg["fields"]:
        value = form_data.get(field["name"], "").strip()
        if value:
            lines.append(f"{field['en']} / {field['ar']}: {value}")
    return "\n".join(lines)


def build_name(req_type_key, employee):
    """Clean human-readable title — no internal markers."""
    cfg = REQUEST_TYPES[req_type_key]
    return f"{cfg['ar']} - {employee['name']}"


def create_approval_request(req_type_key, employee, form_data):
    cfg = REQUEST_TYPES[req_type_key]
    admin_uid = get_admin_uid()
    if not admin_uid:
        raise RuntimeError("Cannot authenticate to Odoo.")

    fields = approval_request_fields()
    desc_field = pick_description_field(fields)
    emp_field = pick_employee_field(fields)

    # If the model has a direct employee link we can filter on that and skip the
    # description marker entirely. Otherwise we still need the marker in the
    # description-like field as a filtering fallback.
    needs_marker = emp_field is None
    body = build_body(req_type_key, employee, form_data, include_marker=needs_marker)

    vals = {
        "name": build_name(req_type_key, employee),
        "request_owner_id": admin_uid,
    }
    if desc_field:
        vals[desc_field] = body
    if emp_field:
        vals[emp_field] = int(employee["id"])

    category_id = find_category_id(req_type_key)
    if category_id:
        vals["category_id"] = category_id

    request_id = odoo_execute("approval.request", "create", [vals])

    # Best-effort: try to submit/confirm so the request leaves draft.
    try:
        odoo_execute("approval.request", "action_confirm", [[request_id]])
    except Exception:
        pass
    return request_id


def _employee_filter_domain(employee_id, available_fields):
    """Build the domain clause that scopes requests to one employee.

    Prefers a direct employee_id field if present; falls back to the marker
    inside the description-like field; final fallback is the marker in `name`
    (covers legacy records created before the title was cleaned up).
    """
    tag = EMP_TAG.format(emp_id=employee_id)
    emp_field = pick_employee_field(available_fields)
    if emp_field:
        return [(emp_field, "=", int(employee_id))]
    desc_field = pick_description_field(available_fields)
    if desc_field:
        return [(desc_field, "ilike", tag)]
    return [("name", "ilike", tag)]


def list_employee_requests(employee_id):
    try:
        available = approval_request_fields()
    except Exception as exc:
        app.logger.error("approval_request_fields failed: %s", exc)
        return []
    domain = _employee_filter_domain(employee_id, available)
    fields = ["id", "name", "request_status", "date_confirmed", "create_date", "category_id"]
    try:
        records = odoo_execute(
            "approval.request",
            "search_read",
            [domain],
            {"fields": fields, "order": "create_date desc", "limit": 100},
        )
    except Exception as exc:
        app.logger.error("list_employee_requests failed: %s", exc)
        return []
    return records


def get_request_detail(employee_id, request_id):
    try:
        available = approval_request_fields()
    except Exception:
        available = {}
    desc_field = pick_description_field(available)
    scope = _employee_filter_domain(employee_id, available)

    detail_fields = ["id", "name", "request_status", "date_confirmed",
                     "create_date", "category_id", "approver_ids"]
    if desc_field:
        detail_fields.append(desc_field)

    domain = [("id", "=", int(request_id))] + scope
    records = odoo_execute(
        "approval.request",
        "search_read",
        [domain],
        {"fields": detail_fields},
    )
    if not records:
        return None, []
    record = records[0]
    # Normalize the description-like field under a stable key for templates.
    record["body"] = record.get(desc_field, "") if desc_field else ""

    messages = []
    try:
        messages = odoo_execute(
            "mail.message",
            "search_read",
            [[("model", "=", "approval.request"), ("res_id", "=", record["id"])]],
            {"fields": ["id", "body", "author_id", "date", "message_type"], "order": "date desc", "limit": 50},
        )
    except Exception:
        messages = []
    return record, messages


# ----------------------------------------------------------------------------
# Auth decorator
# ----------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "employee_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
def _mask(value, keep=4):
    if not value:
        return "(empty)"
    s = str(value)
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "…" + f"({len(s)} chars)"


@app.route("/debug", methods=["GET"])
def debug():
    """Diagnostics: env vars, auth, employee count, sample names, errors."""
    report = {
        "env": {
            "ODOO_URL":      ODOO_URL or "(missing)",
            "ODOO_DB":       ODOO_DB or "(missing)",
            "ODOO_USERNAME": ODOO_USERNAME or "(missing)",
            "ODOO_API_KEY":  _mask(ODOO_API_KEY),
            "all_loaded":    bool(ODOO_URL and ODOO_DB and ODOO_USERNAME and ODOO_API_KEY),
        },
        "auth":          {"uid": None, "error": None},
        "server_info":   {"value": None, "error": None},
        "fields":        {"sample": None, "has_pin": None, "has_barcode": None,
                          "has_employee_id_field": None, "error": None},
        "employees":     {"count_active": None, "count_all": None,
                          "sample": None, "error": None},
        "search_test":   {"empty_query_results": None, "error": None},
        "approval_request": {"fields": None, "picked_description_field": None,
                             "picked_employee_field": None,
                             "employee_like_fields": None,
                             "categories_sample": None, "error": None},
    }

    # Step 1: authentication.
    try:
        common = xmlrpc.client.ServerProxy(
            f"{ODOO_URL}/xmlrpc/2/common", allow_none=True, context=_ssl_context())
        try:
            report["server_info"]["value"] = common.version()
        except Exception as exc:
            report["server_info"]["error"] = repr(exc)
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})
        report["auth"]["uid"] = uid
        if not uid:
            report["auth"]["error"] = "authenticate() returned False/None — wrong DB/username/API key."
    except Exception as exc:
        report["auth"]["error"] = repr(exc)

    # Step 2: fields & employee data.
    if report["auth"]["uid"]:
        try:
            info = odoo_execute("hr.employee", "fields_get", [],
                                {"attributes": ["type", "string"]})
            report["fields"]["sample"] = sorted(list(info.keys()))[:40]
            report["fields"]["has_pin"]              = "pin" in info
            report["fields"]["has_barcode"]          = "barcode" in info
            report["fields"]["has_employee_id_field"] = "employee_id" in info
        except Exception as exc:
            report["fields"]["error"] = repr(exc)

        try:
            report["employees"]["count_active"] = odoo_execute(
                "hr.employee", "search_count", [[]])
            report["employees"]["count_all"] = odoo_execute(
                "hr.employee", "search_count", [[]],
                {"context": {"active_test": False}})
        except Exception as exc:
            report["employees"]["error"] = repr(exc)

        try:
            sample = odoo_execute(
                "hr.employee", "search_read", [[]],
                {"fields": ["id", "name", "job_id", "department_id", "active"],
                 "limit": 3, "context": {"active_test": False}})
            for r in sample:
                r.pop("pin", None)
            report["employees"]["sample"] = sample
        except Exception as exc:
            existing_err = report["employees"].get("error") or ""
            report["employees"]["error"] = (existing_err + " | sample: " + repr(exc)).strip(" |")

        try:
            results = search_employees("")
            for r in results:
                r.pop("pin", None)
            report["search_test"]["empty_query_results"] = results
        except Exception as exc:
            report["search_test"]["error"] = repr(exc)

        try:
            ar_fields = approval_request_fields()
            summary = {k: {"string": v.get("string"), "type": v.get("type")}
                       for k, v in ar_fields.items()
                       if k in (
                           "name", "request_owner_id", "category_id",
                           "request_status", "reason", "note", "description",
                           "request_note", "date_confirmed", "approver_ids",
                           "employee_id", "x_employee_id", "hr_employee_id",
                       )}
            # Also surface every employee-ish field so we don't miss a custom one.
            emp_like = {k: {"string": v.get("string"), "type": v.get("type"),
                            "relation": v.get("relation")}
                        for k, v in ar_fields.items()
                        if "employee" in k.lower()}
            report["approval_request"]["fields"] = {
                "all_names": sorted(ar_fields.keys()),
                "relevant": summary,
            }
            report["approval_request"]["picked_description_field"] = pick_description_field(ar_fields)
            report["approval_request"]["picked_employee_field"] = pick_employee_field(ar_fields)
            report["approval_request"]["employee_like_fields"] = emp_like
        except Exception as exc:
            report["approval_request"]["error"] = repr(exc)

        try:
            cats = odoo_execute(
                "approval.category", "search_read", [[]],
                {"fields": ["id", "name"], "limit": 20})
            report["approval_request"]["categories_sample"] = cats
        except Exception as exc:
            existing = report["approval_request"].get("error") or ""
            report["approval_request"]["error"] = (existing + " | categories: " + repr(exc)).strip(" |")

    pretty = _format_debug_html(report)
    return pretty, 200, {"Content-Type": "text/html; charset=utf-8"}


def _format_debug_html(report):
    import json as _json
    body = _json.dumps(report, indent=2, ensure_ascii=False, default=str)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>HR Portal — Debug</title>
<style>
 body {{ font-family: -apple-system, Segoe UI, monospace; background: #0f172a; color: #e2e8f0; margin: 0; padding: 24px; }}
 h1 {{ color: #22c55e; margin-top: 0; }}
 pre {{ background: #1e293b; padding: 18px; border-radius: 10px;
        white-space: pre-wrap; word-break: break-word; line-height: 1.5; }}
 a {{ color: #86efac; }}
 .note {{ color: #94a3b8; margin-bottom: 14px; font-size: 0.9rem; }}
</style></head>
<body>
<h1>HR Portal — Odoo Debug</h1>
<p class="note">Diagnostics for the Odoo XML-RPC connection. Do NOT expose this route in production once login works. <a href="/login">Back to login</a></p>
<pre>{body}</pre>
</body></html>"""


@app.route("/", methods=["GET"])
def index():
    if "employee_id" in session:
        return redirect(url_for("home"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    step = request.args.get("step", "search")
    candidates = []
    selected_employee = None

    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "search":
                query = request.form.get("query", "").strip()
                if not query:
                    flash("الرجاء إدخال الاسم أو رقم الموظف / Please enter a name or employee number.", "error")
                else:
                    candidates = search_employees(query)
                    for c in candidates:
                        c.pop("pin", None)
                    if not candidates:
                        flash(
                            "لم يتم العثور على موظف. تحقق من /debug للاتصال بـ Odoo. / "
                            "No employee found. Check /debug for Odoo connection details.",
                            "error",
                        )
                    step = "select"

            elif action == "select":
                emp_id = request.form.get("employee_id")
                if emp_id:
                    recs = odoo_execute(
                        "hr.employee",
                        "read",
                        [[int(emp_id)], ["id", "name", "job_title", "department_id"]],
                    )
                    if recs:
                        selected_employee = recs[0]
                        step = "pin"
                    else:
                        flash("الموظف غير موجود / Employee not found.", "error")
                        step = "search"

            elif action == "verify_pin":
                emp_id = request.form.get("employee_id")
                pin = request.form.get("pin", "").strip()
                emp = verify_employee_pin(emp_id, pin)
                if emp:
                    session["employee_id"] = emp["id"]
                    session["employee_name"] = emp["name"]
                    session["employee_job"] = emp.get("job_title") or ""
                    flash(f"مرحبا {emp['name']} / Welcome {emp['name']}", "success")
                    return redirect(url_for("home"))
                else:
                    recs = odoo_execute(
                        "hr.employee",
                        "read",
                        [[int(emp_id)], ["id", "name", "job_title"]],
                    )
                    selected_employee = recs[0] if recs else None
                    flash("الرقم السري غير صحيح / Invalid PIN.", "error")
                    step = "pin"
        except Exception as exc:
            app.logger.exception("Login error")
            flash(f"خطأ في الاتصال بـ Odoo / Odoo connection error: {exc}", "error")
            step = "search"

    return render_template(
        "login.html",
        step=step,
        candidates=candidates,
        selected_employee=selected_employee,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/home")
@login_required
def home():
    return render_template(
        "home.html",
        employee_name=session.get("employee_name"),
        employee_job=session.get("employee_job"),
        request_types=REQUEST_TYPES,
    )


@app.route("/request/<req_type>", methods=["GET", "POST"])
@login_required
def request_form(req_type):
    if req_type not in REQUEST_TYPES:
        abort(404)
    cfg = REQUEST_TYPES[req_type]

    if request.method == "POST":
        form_data = {f["name"]: request.form.get(f["name"], "").strip() for f in cfg["fields"]}
        missing = [f for f in cfg["fields"] if f.get("required") and not form_data.get(f["name"])]
        if missing:
            flash("الرجاء تعبئة الحقول المطلوبة / Please fill required fields.", "error")
            return render_template(
                "request_form.html",
                req_type=req_type,
                cfg=cfg,
                form_data=form_data,
            )

        employee = {
            "id": session["employee_id"],
            "name": session["employee_name"],
        }
        try:
            create_approval_request(req_type, employee, form_data)
            flash("تم إرسال الطلب بنجاح / Request submitted successfully.", "success")
            return redirect(url_for("my_requests"))
        except Exception as exc:
            app.logger.exception("Create request failed")
            flash(f"فشل إرسال الطلب / Failed to submit request: {exc}", "error")

    return render_template(
        "request_form.html",
        req_type=req_type,
        cfg=cfg,
        form_data={},
    )


@app.route("/my-requests")
@login_required
def my_requests():
    records = list_employee_requests(session["employee_id"])
    return render_template(
        "my_requests.html",
        records=records,
    )


@app.route("/request-detail/<int:request_id>")
@login_required
def request_detail(request_id):
    record, messages = get_request_detail(session["employee_id"], request_id)
    if not record:
        abort(404)
    return render_template(
        "request_detail.html",
        record=record,
        messages=messages,
    )


import re as _re
_TAG_RE = _re.compile(r"\[HR-PORTAL-EMP:\d+\]\s*")


@app.template_filter("strip_tag")
def strip_tag(value):
    """Remove the internal [HR-PORTAL-EMP:N] marker from a request title."""
    if not value:
        return ""
    return _TAG_RE.sub("", str(value)).strip()


@app.template_filter("m2o_name")
def m2o_name(value):
    """Return the display name of an Odoo many2one tuple, [id, "Name"]."""
    if not value:
        return ""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return value[1]
    return str(value)


@app.template_filter("fmt_date")
def fmt_date(value):
    if not value:
        return ""
    try:
        if isinstance(value, str):
            dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
        else:
            dt = value
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


@app.template_filter("status_label")
def status_label(value):
    mapping = {
        "new": ("جديد", "New", "status-new"),
        "pending": ("قيد المراجعة", "Pending", "status-pending"),
        "under_approval": ("قيد الاعتماد", "Under Approval", "status-pending"),
        "approved": ("معتمد", "Approved", "status-approved"),
        "refused": ("مرفوض", "Refused", "status-refused"),
        "cancel": ("ملغي", "Cancelled", "status-cancel"),
    }
    return mapping.get(value, (value or "—", value or "—", "status-new"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_ENV") != "production")

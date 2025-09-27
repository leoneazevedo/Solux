#!/usr/bin/env python3
"""
SOLUX simple MVP web server

This script implements a minimal web application that satisfies the core
requirements described in the user's specification.  It uses only Python
standard library modules to provide a complete, self‑contained solution
without any external dependencies.  The server stores data in an SQLite
database, writes uploaded files into a local ``uploads/`` directory and
serves both the HTML user interface and the JSON API described in the
specification.

Major features implemented:

* User authentication with a session cookie (one hard‑coded user for the
  MVP).  Login and logout pages are available.
* CRUD operations for customers.  New customers can be created and
  existing ones can be viewed and edited while in the ``EM_ANDAMENTO``
  state.
* File uploads for customer documents, utility bills and final budgets.
  The HTML forms use ``input type="file"`` with the ``capture`` attribute
  to open the device camera when supported by mobile browsers.  Files are
  stored on disk and metadata is recorded in the database.
* Closing a deal: when a customer transitions from ``EM_ANDAMENTO`` to
  ``FECHADO`` the user must provide financial information, the project
  details and upload the final budget document.  Instalments are
  automatically generated based on the number of parcels and their
  payment dates.
* Audit logging: every meaningful action creates an entry in the
  ``audit_logs`` table recording who performed the action and when.
* Simple filtering/searching on the customer list page by status and
  substring match on the customer's name or address.
* REST API endpoints returning JSON for programmatic access.  The same
  handlers that render HTML pages can also emit JSON when the ``Accept:
  application/json`` header is set.

Because this server relies only on the standard library it is not
intended to be highly concurrent or highly performant.  It is designed
for demonstration purposes and as a starting point for a more
production‑ready implementation using proper frameworks and external
services for storage and authentication.  Nevertheless, all of the core
functionalities requested are present and should work out of the box.
"""

import base64
import cgi
import hashlib
import io
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import uuid
from datetime import datetime
from http import cookies
from http import HTTPStatus
from http.server import HTTPServer, SimpleHTTPRequestHandler
from typing import Dict, List, Tuple, Optional


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "solux.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

# Ensure the uploads directory exists
os.makedirs(UPLOAD_DIR, exist_ok=True)


def init_db():
    """Initialise the SQLite database with the required tables."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            name TEXT NOT NULL,
            address TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('EM_ANDAMENTO','FECHADO'))
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_files (
            id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('DOCUMENTO','CONTA_LUZ','ORCAMENTO_FINAL')),
            file_name TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS closed_deals (
            id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL UNIQUE,
            valor_entrada REAL NOT NULL CHECK(valor_entrada >= 0),
            valor_parcela REAL NOT NULL CHECK(valor_parcela >= 0),
            quantidade_parcelas INTEGER NOT NULL CHECK(quantidade_parcelas >= 0),
            data_pagamento_entrada TEXT NOT NULL,
            inversor TEXT NOT NULL,
            quantidade_placas INTEGER NOT NULL CHECK(quantidade_placas >= 1),
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS parcelas (
            id TEXT PRIMARY KEY,
            closed_deal_id TEXT NOT NULL,
            numero_parcela INTEGER NOT NULL,
            data_pagamento TEXT NOT NULL,
            FOREIGN KEY(closed_deal_id) REFERENCES closed_deals(id)
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        )
    """
    )
    conn.commit()
    # Add new fields to closed_deals for extended financial information.
    # SQLite does not support IF NOT EXISTS for columns, so attempt to alter
    # the table and ignore errors if the columns already exist.
    try:
        cur.execute("ALTER TABLE closed_deals ADD COLUMN valor_total_projeto REAL")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        cur.execute("ALTER TABLE closed_deals ADD COLUMN custo_projeto_total REAL")
    except sqlite3.OperationalError:
        pass
    # Add pago column to parcelas to track if a parcel has been paid
    try:
        cur.execute("ALTER TABLE parcelas ADD COLUMN pago INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE closed_deals ADD COLUMN lucro_venda REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE closed_deals ADD COLUMN observacao TEXT")
    except sqlite3.OperationalError:
        pass

    # Extended financial fields requested by the user.  These may not exist on
    # older versions of the database, so attempt to add them here.  Ignore
    # errors if the column already exists.
    try:
        cur.execute("ALTER TABLE closed_deals ADD COLUMN juros_percentual REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE closed_deals ADD COLUMN valor_total_financiamento REAL")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE closed_deals ADD COLUMN observacao_custos TEXT")
    except sqlite3.OperationalError:
        pass
    conn.close()


def render_template(name: str, context: Dict[str, str]) -> bytes:
    """Render an HTML template with simple substitution.

    The templates are stored in the ``templates`` directory and use
    ``{{placeholder}}`` syntax for substitution.  This function performs
    a straightforward ``str.replace`` for each key in the context.
    """
    path = os.path.join(TEMPLATE_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    for key, value in context.items():
        placeholder = "{{" + key + "}}"
        html = html.replace(placeholder, value)
    return html.encode("utf-8")


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


class SessionStore:
    """Simple in‑memory session store keyed by a session ID."""

    def __init__(self):
        self.sessions: Dict[str, Dict[str, str]] = {}

    def create_session(self, user_id: str) -> str:
        sid = str(uuid.uuid4())
        self.sessions[sid] = {"user_id": user_id}
        return sid

    def get_user(self, sid: str) -> Optional[str]:
        sess = self.sessions.get(sid)
        return sess.get("user_id") if sess else None

    def destroy_session(self, sid: str) -> None:
        if sid in self.sessions:
            del self.sessions[sid]


SESSION_STORE = SessionStore()


def require_login(func):
    """Decorator to ensure the user is authenticated before calling handler."""

    def wrapper(self, *args, **kwargs):
        user_id = self.current_user
        if not user_id:
            self.redirect("/login")
            return
        return func(self, *args, **kwargs)

    return wrapper


class SoluxHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for the SOLUX application."""

    server_version = "SoluxHTTP/0.1"

    def __init__(self, *args, **kwargs):
        self.db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db_conn.row_factory = sqlite3.Row
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------
    def log_message(self, format: str, *args) -> None:
        """Override to print to stderr with timestamp."""
        sys_msg = f"[{datetime.now().isoformat()}] {self.address_string()} - " + format % args
        print(sys_msg)

    @property
    def current_user(self) -> Optional[str]:
        """Retrieve the current logged in user_id based on the session cookie."""
        if not hasattr(self, "_current_user"):
            self._current_user = None
            if "Cookie" in self.headers:
                cookie = cookies.SimpleCookie(self.headers.get("Cookie"))
                sid = cookie.get("session_id")
                if sid:
                    self._current_user = SESSION_STORE.get_user(sid.value)
        return self._current_user

    def set_session_cookie(self, session_id: str) -> None:
        cookie = cookies.SimpleCookie()
        cookie["session_id"] = session_id
        cookie["session_id"]["path"] = "/"
        self.send_header("Set-Cookie", cookie.output(header="", sep=""))

    def clear_session_cookie(self) -> None:
        cookie = cookies.SimpleCookie()
        cookie["session_id"] = ""
        cookie["session_id"]["path"] = "/"
        cookie["session_id"]["max-age"] = 0
        self.send_header("Set-Cookie", cookie.output(header="", sep=""))

    def parse_post_data(self) -> Tuple[Dict[str, str], List[Tuple[str, dict]]]:
        """Parse POST form data including file uploads.

        Returns a tuple ``(fields, files)`` where ``fields`` is a mapping
        from field names to string values and ``files`` is a list of
        tuples ``(field_name, file_info)``.  Each ``file_info`` is a
        dictionary with keys ``filename``, ``content_type`` and ``data``.
        """
        ctype, pdict = cgi.parse_header(self.headers.get("Content-Type", ""))
        fields: Dict[str, str] = {}
        files: List[Tuple[str, dict]] = []
        if ctype == "multipart/form-data":
            pdict['boundary'] = pdict['boundary'].encode("utf-8") if isinstance(pdict['boundary'], str) else pdict['boundary']
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={'REQUEST_METHOD': 'POST'},
                keep_blank_values=True
            )
            for key in form:
                item = form[key]
                if isinstance(item, list):
                    # multiple files/fields with same name
                    for subitem in item:
                        if subitem.filename:
                            files.append((key, {
                                'filename': subitem.filename,
                                'content_type': subitem.type,
                                'data': subitem.file.read(),
                            }))
                        else:
                            fields[key] = subitem.value
                else:
                    if item.filename:
                        files.append((key, {
                            'filename': item.filename,
                            'content_type': item.type,
                            'data': item.file.read(),
                        }))
                    else:
                        fields[key] = item.value
        elif ctype == "application/x-www-form-urlencoded":
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            fields = dict(urllib.parse.parse_qsl(body))
        else:
            # Unsupported content type
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            try:
                fields = json.loads(body)
            except json.JSONDecodeError:
                fields = {}
        return fields, files

    def send_json(self, data: Dict, status: int = 200) -> None:
        """Helper to send a JSON response."""
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, path: str) -> None:
        """Helper to send a redirect response."""
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header('Location', path)
        self.end_headers()

    # ------------------------------------------------------------------
    # Request handlers (routing)
    # ------------------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        # Static files under /static
        if path.startswith('/static/'):
            return super().do_GET()
        elif path.startswith('/file/'):
            return self.handle_file_serving(path)
        elif path.startswith('/parcels/'):
            # Endpoint to toggle parcel payment status
            parts = path.split('/')
            # Expect format: /parcels/<parcel_id>/toggle
            if len(parts) == 4 and parts[3] == 'toggle':
                parcel_id = parts[2]
                return self.handle_parcel_toggle_get(parcel_id)
        elif path == '/':
            return self.handle_root()
        elif path == '/login':
            return self.handle_login_get()
        elif path == '/logout':
            return self.handle_logout()
        elif path == '/customers':
            return self.handle_customers_list(parsed.query)
        elif path == '/customers/new':
            return self.handle_customer_new_get()
        elif path.startswith('/customers/'):
            parts = path.split('/')
            if len(parts) >= 3:
                cust_id = parts[2]
                subpath = parts[3:] if len(parts) > 3 else []
                if not subpath:
                    return self.handle_customer_detail_get(cust_id)
                elif subpath == ['close']:
                    return self.handle_customer_close_get(cust_id)
                elif subpath == ['files']:
                    return self.handle_customer_files_get(cust_id)
                elif subpath == ['audit']:
                    return self.handle_customer_audit_get(cust_id)
        # Unknown path
        self.send_error(HTTPStatus.NOT_FOUND, "File not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        if path == '/login':
            return self.handle_login_post()
        elif path == '/customers/new':
            return self.handle_customer_new_post()
        elif path.startswith('/customers/'):
            parts = path.split('/')
            if len(parts) >= 3:
                cust_id = parts[2]
                subpath = parts[3:] if len(parts) > 3 else []
                if not subpath:
                    return self.handle_customer_update_post(cust_id)
                elif subpath == ['upload']:
                    return self.handle_customer_upload_post(cust_id)
                elif subpath == ['close']:
                    return self.handle_customer_close_post(cust_id)
        # Unknown path
        self.send_error(HTTPStatus.NOT_FOUND, "File not found")

    # ------------------------------------------------------------------
    # Handler implementations
    # ------------------------------------------------------------------
    def handle_root(self):
        if self.current_user:
            self.redirect('/customers')
        else:
            self.redirect('/login')

    def handle_login_get(self):
        # Render login form
        html = render_template('login.html', {'error': ''})
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def handle_login_post(self):
        fields, _ = self.parse_post_data()
        username = fields.get('username', '')
        password = fields.get('password', '')
        # For MVP, a single user defined by environment variables or defaults
        admin_user = os.environ.get('SOLUX_ADMIN_USER', 'admin')
        admin_pass_hash = os.environ.get('SOLUX_ADMIN_PASS_HASH', hash_password('solux'))
        if username == admin_user and hash_password(password) == admin_pass_hash:
            sid = SESSION_STORE.create_session(user_id=username)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.set_session_cookie(sid)
            self.send_header('Location', '/customers')
            self.end_headers()
        else:
            html = render_template('login.html', {'error': 'Credenciais inválidas'})
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    def handle_logout(self):
        sid = None
        if 'Cookie' in self.headers:
            cookie = cookies.SimpleCookie(self.headers.get('Cookie'))
            sid_cookie = cookie.get('session_id')
            sid = sid_cookie.value if sid_cookie else None
        if sid:
            SESSION_STORE.destroy_session(sid)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.clear_session_cookie()
        self.send_header('Location', '/login')
        self.end_headers()

    @require_login
    def handle_customers_list(self, query: str):
        # Parse query parameters
        params = urllib.parse.parse_qs(query)
        status_filter = params.get('status', [''])[0]
        search = params.get('q', [''])[0].lower()
        # Fetch customers from DB
        cur = self.db_conn.cursor()
        sql = "SELECT * FROM customers"
        where_clauses = []
        args = []
        if status_filter in ('EM_ANDAMENTO', 'FECHADO'):
            where_clauses.append("status = ?")
            args.append(status_filter)
        if search:
            where_clauses.append("(lower(name) LIKE ? OR lower(address) LIKE ?)")
            args.extend([f"%{search}%", f"%{search}%"])
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)
        sql += " ORDER BY created_at DESC"
        cur.execute(sql, args)
        customers = cur.fetchall()
        # Determine if JSON API requested
        if 'application/json' in self.headers.get('Accept', ''):
            data = []
            for cust in customers:
                data.append({k: cust[k] for k in cust.keys()})
            return self.send_json({'customers': data})
        # Render HTML
        rows_html = ""
        for cust in customers:
            rows_html += f"<tr><td><a href='/customers/{cust['id']}' class='text-blue-600 underline'>{cust['name']}</a></td>"
            rows_html += f"<td>{cust['address']}</td><td>{cust['status']}</td></tr>"
        context = {
            'rows': rows_html or "<tr><td colspan='3'>Nenhum cliente encontrado</td></tr>",
            'status_filter': status_filter,
            'search': search,
            'current_user': self.current_user,
        }
        html = render_template('customers_list.html', context)
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    @require_login
    def handle_customer_new_get(self):
        html = render_template('customer_form.html', {
            'action': '/customers/new',
            'name': '',
            'address': '',
            'error': '',
            'button_label': 'Criar Cliente',
            'show_close': 'false',
        })
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    @require_login
    def handle_customer_new_post(self):
        fields, _ = self.parse_post_data()
        name = fields.get('name', '').strip()
        address = fields.get('address', '').strip()
        if not name or not address:
            html = render_template('customer_form.html', {
                'action': '/customers/new',
                'name': name,
                'address': address,
                'error': 'Nome e endereço são obrigatórios.',
                'button_label': 'Criar Cliente',
                'show_close': 'false',
            })
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        cid = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        cur = self.db_conn.cursor()
        cur.execute(
            "INSERT INTO customers (id, created_at, updated_at, name, address, status)"
            " VALUES (?, ?, ?, ?, ?, 'EM_ANDAMENTO')",
            (cid, now, now, name, address)
        )
        self.db_conn.commit()
        # Audit log
        self.log_action(cid, 'CREATE_CUSTOMER', {'name': name, 'address': address})
        self.redirect(f'/customers/{cid}')

    @require_login
    def handle_customer_detail_get(self, cid: str):
        cur = self.db_conn.cursor()
        cust = cur.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
        if not cust:
            self.send_error(HTTPStatus.NOT_FOUND, "Cliente não encontrado")
            return
        # Files
        files = cur.execute(
            "SELECT * FROM customer_files WHERE customer_id = ? ORDER BY created_at ASC",
            (cid,)
        ).fetchall()
        # Closed deal (if any)
        closed = cur.execute(
            "SELECT * FROM closed_deals WHERE customer_id = ?",
            (cid,)
        ).fetchone()
        parcels = []
        if closed:
            parcels = cur.execute(
                "SELECT * FROM parcelas WHERE closed_deal_id = ? ORDER BY numero_parcela ASC",
                (closed['id'],)
            ).fetchall()
        # Check Accept header for JSON
        if 'application/json' in self.headers.get('Accept', ''):
            data = {k: cust[k] for k in cust.keys()}
            data['files'] = [dict(row) for row in files]
            if closed:
                data['closed_deal'] = {k: closed[k] for k in closed.keys()}
                data['parcels'] = [dict(row) for row in parcels]
            return self.send_json({'customer': data})
        # Build HTML for files
        files_html = ""
        for f in files:
            icon = '📄'
            files_html += f"<li>{icon} {f['type']} - <a href='/file/{f['id']}' class='underline text-blue-600'>{f['file_name']}</a></li>"
        if not files_html:
            files_html = "<p>Nenhum arquivo anexado.</p>"
        # Build HTML for parcels if closed
        parcels_html = ""
        if parcels:
            parcels_html = "<ul>"
            for p in parcels:
                # Determine value string
                valor_parcela_str = ''
                if closed and closed['valor_parcela'] is not None:
                    valor_parcela_str = f" - valor R$ {closed['valor_parcela']:.2f}"
                # Payment status and toggle link
                pago_label = 'Pago' if (p['pago'] or 0) else 'Em aberto'
                toggle_text = 'Marcar não pago' if (p['pago'] or 0) else 'Marcar pago'
                toggle_link = f"/parcels/{p['id']}/toggle"
                parcels_html += (
                    f"<li>Parcela {p['numero_parcela']}: pagamento em {p['data_pagamento']}{valor_parcela_str} - "
                    f"{pago_label} <a href='{toggle_link}'>{toggle_text}</a></li>"
                )
            parcels_html += "</ul>"
        # Build closing summary if closed
        close_summary = ""
        if closed:
            parts = []
            # Show optional extended fields
            if closed['valor_total_projeto'] is not None:
                parts.append(f"<strong>Valor total do projeto:</strong> R$ {closed['valor_total_projeto']:.2f}")
            if closed['custo_projeto_total'] is not None:
                parts.append(f"<strong>Custo total do projeto:</strong> R$ {closed['custo_projeto_total']:.2f}")
            if closed['observacao_custos']:
                parts.append(f"<strong>Obs. custos:</strong> {closed['observacao_custos']}")
            if closed['lucro_venda'] is not None:
                parts.append(f"<strong>Lucro pela venda:</strong> R$ {closed['lucro_venda']:.2f}")
            if closed['valor_total_financiamento'] is not None:
                parts.append(f"<strong>Valor total (financiado):</strong> R$ {closed['valor_total_financiamento']:.2f}")
            parts.append(f"<strong>Entrada:</strong> R$ {closed['valor_entrada']:.2f}")
            parts.append(f"<strong>Valor de cada parcela:</strong> R$ {closed['valor_parcela']:.2f}")
            parts.append(f"<strong>Quantidade de parcelas:</strong> {closed['quantidade_parcelas']}")
            if closed['juros_percentual'] is not None:
                parts.append(f"<strong>Juros aplicado:</strong> {closed['juros_percentual']:.2f}%")
            parts.append(f"<strong>Data pagamento da entrada:</strong> {closed['data_pagamento_entrada']}")
            parts.append(f"<strong>Inversor:</strong> {closed['inversor'] or ''}")
            parts.append(f"<strong>Quantidade de placas:</strong> {closed['quantidade_placas']}")
            if closed['observacao']:
                parts.append(f"<strong>Observações:</strong> {closed['observacao']}")
            close_summary = '<p>' + '<br>'.join(parts) + '</p>'
        # Determine which template to use based on status
        if cust['status'] == 'FECHADO':
            html = render_template('customer_closed.html', {
                'name': cust['name'],
                'address': cust['address'],
                'close_summary': close_summary,
                'parcels': parcels_html or '<p>Sem parcelas.</p>',
                'files': files_html,
                'customer_id': cid,
            })
        else:
            html = render_template('customer_detail.html', {
                'name': cust['name'],
                'address': cust['address'],
                'customer_id': cid,
                'files': files_html,
                'error': '',
            })
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    @require_login
    def handle_customer_update_post(self, cid: str):
        fields, _ = self.parse_post_data()
        name = fields.get('name', '').strip()
        address = fields.get('address', '').strip()
        if not name or not address:
            # re-render with error
            cur = self.db_conn.cursor()
            cust = cur.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
            files = cur.execute(
                "SELECT * FROM customer_files WHERE customer_id = ?", (cid,)
            ).fetchall()
            files_html = ""
            for f in files:
                files_html += f"<li>{f['type']} - <a href='/file/{f['id']}'>{f['file_name']}</a></li>"
            html = render_template('customer_detail.html', {
                'name': name or cust['name'],
                'address': address or cust['address'],
                'customer_id': cid,
                'files': files_html or '<p>Nenhum arquivo anexado.</p>',
                'error': 'Nome e endereço são obrigatórios.',
            })
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        cur = self.db_conn.cursor()
        cur.execute(
            "UPDATE customers SET name = ?, address = ?, updated_at = ? WHERE id = ?",
            (name, address, datetime.utcnow().isoformat(), cid)
        )
        self.db_conn.commit()
        self.log_action(cid, 'UPDATE_CUSTOMER', {'name': name, 'address': address})
        self.redirect(f'/customers/{cid}')

    @require_login
    def handle_customer_upload_post(self, cid: str):
        # handle file uploads for DOCUMENTO or CONTA_LUZ or ORCAMENTO_FINAL
        fields, files = self.parse_post_data()
        file_type = fields.get('file_type', '')
        if file_type not in ('DOCUMENTO', 'CONTA_LUZ', 'ORCAMENTO_FINAL'):
            return self.send_json({'error': 'Tipo de arquivo inválido'}, status=400)
        if not files:
            return self.send_json({'error': 'Nenhum arquivo enviado'}, status=400)
        cur = self.db_conn.cursor()
        for _, file_info in files:
            file_id = str(uuid.uuid4())
            filename = file_info['filename']
            mime_type = file_info['content_type'] or 'application/octet-stream'
            data = file_info['data']
            size = len(data)
            # Save to disk
            safe_name = f"{file_id}_{filename}"
            file_path = os.path.join(UPLOAD_DIR, safe_name)
            with open(file_path, 'wb') as f:
                f.write(data)
            # Store metadata
            cur.execute(
                "INSERT INTO customer_files (id, customer_id, type, file_name, mime_type, size_bytes, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (file_id, cid, file_type, filename, mime_type, size, datetime.utcnow().isoformat())
            )
            # Audit
            self.log_action(cid, 'UPLOAD_FILE', {'file_type': file_type, 'file_name': filename})
        self.db_conn.commit()
        # If HTML form, redirect back; else send JSON
        if 'application/json' in self.headers.get('Accept', ''):
            return self.send_json({'success': True})
        self.redirect(f'/customers/{cid}')

    @require_login
    def handle_customer_files_get(self, cid: str):
        # Return JSON list of files
        cur = self.db_conn.cursor()
        files = cur.execute(
            "SELECT * FROM customer_files WHERE customer_id = ? ORDER BY created_at ASC",
            (cid,)
        ).fetchall()
        data = [dict(row) for row in files]
        return self.send_json({'files': data})

    @require_login
    def handle_customer_audit_get(self, cid: str):
        cur = self.db_conn.cursor()
        logs = cur.execute(
            "SELECT * FROM audit_logs WHERE customer_id = ? ORDER BY created_at DESC",
            (cid,)
        ).fetchall()
        data = []
        for log in logs:
            record = {k: log[k] for k in log.keys()}
            try:
                record['details'] = json.loads(record['details']) if record['details'] else None
            except json.JSONDecodeError:
                record['details'] = record['details']
            data.append(record)
        return self.send_json({'audit': data})

    def handle_parcel_toggle_get(self, parcel_id: str):
        """Toggle the payment status of a parcel and redirect back to the customer detail page.

        This endpoint flips the ``pago`` flag of the specified parcel.  After
        updating the record, it redirects the user back to the corresponding
        customer detail page so they can view the updated status.  If the
        parcel or associated customer cannot be found, a 404 error is
        returned.
        """
        # Require authentication
        if not self.current_user:
            self.redirect('/login')
            return
        cur = self.db_conn.cursor()
        p = cur.execute("SELECT * FROM parcelas WHERE id = ?", (parcel_id,)).fetchone()
        if not p:
            self.send_error(HTTPStatus.NOT_FOUND, "Parcela não encontrada")
            return
        # Toggle pago flag: if 1 then 0, if 0 or null then 1
        current_status = p['pago'] or 0
        new_status = 0 if current_status else 1
        cur.execute("UPDATE parcelas SET pago = ? WHERE id = ?", (new_status, parcel_id))
        # Determine the customer id via the closed_deal
        closed = cur.execute("SELECT customer_id FROM closed_deals WHERE id = ?", (p['closed_deal_id'],)).fetchone()
        cid = closed['customer_id'] if closed else None
        self.db_conn.commit()
        if cid:
            self.redirect(f'/customers/{cid}')
        else:
            self.redirect('/customers')

    @require_login
    def handle_customer_close_get(self, cid: str):
        # Render closing or editing form for a customer.  If the customer is still
        # in andamento, the fields are empty; if already closed, prefill with existing values.
        cur = self.db_conn.cursor()
        cust = cur.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
        if not cust:
            self.send_error(HTTPStatus.NOT_FOUND, "Cliente não encontrado")
            return
        closed = cur.execute("SELECT * FROM closed_deals WHERE customer_id = ?", (cid,)).fetchone()
        # Prepare default values or prefill from existing record
        context = {
            'customer_id': cid,
            'error': '',
            'valor_total_projeto': '',
            'custo_projeto_total': '',
            'lucro_venda': '',
            'valor_total_financiamento': '',
            'juros_percentual': '',
            'valor_entrada': '',
            'valor_parcela': '',
            'quantidade_parcelas': '',
            'data_pagamento_entrada': '',
            'parcelas_datas': '',
            'inversor': '',
            'quantidade_placas': '',
            'observacao_custos': '',
            'observacao': '',
        }
        if closed:
            # Prefill existing values
            context['valor_total_projeto'] = '' if closed['valor_total_projeto'] is None else f"{closed['valor_total_projeto']:.2f}"
            context['custo_projeto_total'] = '' if closed['custo_projeto_total'] is None else f"{closed['custo_projeto_total']:.2f}"
            context['lucro_venda'] = '' if closed['lucro_venda'] is None else f"{closed['lucro_venda']:.2f}"
            context['valor_total_financiamento'] = '' if closed['valor_total_financiamento'] is None else f"{closed['valor_total_financiamento']:.2f}"
            context['juros_percentual'] = '' if closed['juros_percentual'] is None else f"{closed['juros_percentual']:.2f}"
            context['valor_entrada'] = f"{closed['valor_entrada']:.2f}"
            context['valor_parcela'] = f"{closed['valor_parcela']:.2f}"
            context['quantidade_parcelas'] = str(closed['quantidade_parcelas'])
            context['data_pagamento_entrada'] = closed['data_pagamento_entrada']
            # Parcels
            parcelas = cur.execute(
                "SELECT data_pagamento FROM parcelas WHERE closed_deal_id = ? ORDER BY numero_parcela ASC",
                (closed['id'],)
            ).fetchall()
            context['parcelas_datas'] = ', '.join([p['data_pagamento'] for p in parcelas])
            context['inversor'] = closed['inversor']
            context['quantidade_placas'] = str(closed['quantidade_placas'])
            context['observacao_custos'] = closed['observacao_custos'] or ''
            context['observacao'] = closed['observacao'] or ''
        html = render_template('customer_close.html', context)
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    @require_login
    def handle_customer_close_post(self, cid: str):
        # Handle closing a customer
        fields, files = self.parse_post_data()
        # Parse numeric values (optional); treat empty string as None
        def parse_optional_float(value: str) -> Optional[float]:
            try:
                return float(value) if value not in (None, '',) else None
            except ValueError:
                return None
        def parse_optional_int(value: str) -> Optional[int]:
            try:
                return int(value) if value not in (None, '',) else None
            except ValueError:
                return None
        # Financial and project inputs.  Some fields may be left empty (None).
        valor_total = parse_optional_float(fields.get('valor_total_projeto'))
        custo_total = parse_optional_float(fields.get('custo_projeto_total'))
        # Lucro de venda: if provided directly, parse; otherwise will be computed later
        lucro_venda_input = parse_optional_float(fields.get('lucro_venda'))
        # Entrada e parcelas: default to 0 if missing
        valor_entrada = parse_optional_float(fields.get('valor_entrada')) or 0.0
        quantidade_parcelas = parse_optional_int(fields.get('quantidade_parcelas')) or 0
        # Taxa de juros (percentual) aplicada nas parcelas
        juros_percentual = parse_optional_float(fields.get('juros_percentual'))
        # We do not accept a user‑supplied parcel value; it will be computed if juros_percentual and valor_total exist
        valor_parcela_input = parse_optional_float(fields.get('valor_parcela'))
        data_pagamento_entrada = fields.get('data_pagamento_entrada', '')
        inversor = fields.get('inversor', '').strip()
        quantidade_placas_raw = parse_optional_int(fields.get('quantidade_placas'))
        observacao_custos = fields.get('observacao_custos', '').strip()
        observacao = fields.get('observacao', '').strip()
        # Parse parcel dates.  If the user did not provide explicit dates, generate them
        # automatically based on the entry payment date (monthly intervals).
        parcelas_datas = fields.get('parcelas_datas', '')
        parcel_dates: List[str] = []
        if isinstance(parcelas_datas, str) and parcelas_datas.strip():
            parcel_dates = [d.strip() for d in parcelas_datas.split(',') if d.strip()]
        # If no explicit dates were provided and we have a positive number of parcels,
        # build a sequence of due dates by adding months to the entry date.  If
        # data_pagamento_entrada is missing or invalid, default to today's date.
        if not parcel_dates and quantidade_parcelas > 0:
            # Determine starting date
            from datetime import datetime as _dt, date as _date
            start_date: _date
            try:
                # Accept YYYY-MM-DD format; if invalid will go to except
                start_date = _dt.strptime(data_pagamento_entrada, '%Y-%m-%d').date() if data_pagamento_entrada else _dt.utcnow().date()
            except Exception:
                start_date = _dt.utcnow().date()
            def add_months(src: _date, m: int) -> _date:
                # Compute year and month after adding m months
                year = src.year + (src.month + m - 1) // 12
                month = (src.month + m - 1) % 12 + 1
                # Last day of target month
                from calendar import monthrange
                last_day = monthrange(year, month)[1]
                day = min(src.day, last_day)
                return _date(year, month, day)
            for i in range(1, quantidade_parcelas + 1):
                due = add_months(start_date, i)
                parcel_dates.append(due.isoformat())
        # Compute derived values
        # Lucro de venda: prefer explicit value; otherwise compute from valor_total e custo_total.
        if lucro_venda_input is not None:
            lucro_venda = lucro_venda_input
        else:
            lucro_venda = (valor_total - custo_total) if (valor_total is not None and custo_total is not None) else None

        # Calculate financing totals.  When the user provides a total project value, a number of parcels
        # and an interest rate, compute the financed amount and each parcel value.  Otherwise use
        # user‑supplied parcel value if provided, or default to 0.
        valor_parcela: float = 0.0
        valor_total_financiamento: Optional[float] = None
        if (valor_total is not None and juros_percentual is not None and quantidade_parcelas > 0):
            # Remaining amount after the down payment
            remaining = valor_total - valor_entrada
            # If remaining is negative, treat it as zero
            if remaining < 0:
                remaining = 0.0
            # Convert juros_percentual from percent to fraction
            rate_fraction = juros_percentual / 100.0
            montante = remaining * (1.0 + rate_fraction)
            valor_parcela = montante / quantidade_parcelas
            valor_total_financiamento = valor_entrada + montante
        else:
            # Use provided parcel value if available, else 0
            valor_parcela = valor_parcela_input or 0.0
            valor_total_financiamento = None

        # Validate some constraints
        errors: List[str] = []
        if valor_entrada is not None and valor_entrada < 0:
            errors.append('Valor da entrada não pode ser negativo.')
        if quantidade_parcelas < 0:
            errors.append('Quantidade de parcelas deve ser zero ou positiva.')
        # Validate numeric conversions: custo_total, valor_total, lucro_venda may be None; that's ok
        if errors:
            return self.send_error_message_close(cid, '\n'.join(errors))
        # Determine if this is an update or a new closure
        cur = self.db_conn.cursor()
        cust = cur.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
        if not cust:
            return self.send_error_message_close(cid, 'Cliente não encontrado.')
        existing_closed = cur.execute("SELECT * FROM closed_deals WHERE customer_id = ?", (cid,)).fetchone()
        # If new closure and customer is not yet closed, we must at least set default values for inversor and quantidade_placas
        if not existing_closed and cust['status'] != 'EM_ANDAMENTO':
            return self.send_error_message_close(cid, 'Cliente já está fechado.')
        # Determine closed_deal id
        if existing_closed:
            closed_id = existing_closed['id']
            # Determine final quantity of panels to store
            quantidade_placas_val = (
                existing_closed['quantidade_placas']
                if (quantidade_placas_raw is None or quantidade_placas_raw <= 0)
                else quantidade_placas_raw
            )
            # Update fields, including new financial data
            cur.execute(
                "UPDATE closed_deals SET valor_total_projeto = ?, custo_projeto_total = ?, lucro_venda = ?, valor_entrada = ?, valor_parcela = ?, quantidade_parcelas = ?, data_pagamento_entrada = ?, inversor = ?, quantidade_placas = ?, observacao_custos = ?, observacao = ?, juros_percentual = ?, valor_total_financiamento = ? WHERE id = ?",
                (
                    valor_total,
                    custo_total,
                    lucro_venda,
                    valor_entrada,
                    valor_parcela,
                    quantidade_parcelas,
                    data_pagamento_entrada,
                    inversor,
                    quantidade_placas_val,
                    observacao_custos,
                    observacao,
                    juros_percentual,
                    valor_total_financiamento,
                    closed_id,
                ),
            )
            # Delete existing parcelas and recreate based on new dates
            cur.execute("DELETE FROM parcelas WHERE closed_deal_id = ?", (closed_id,))
            for idx, date_str in enumerate(parcel_dates, start=1):
                parcel_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO parcelas (id, closed_deal_id, numero_parcela, data_pagamento, pago) VALUES (?, ?, ?, ?, ?)",
                    (parcel_id, closed_id, idx, date_str, 0)
                )
        else:
            # Create new closed_deal
            closed_id = str(uuid.uuid4())
            # Determine final quantity of panels
            quantidade_placas_val = (
                1 if (quantidade_placas_raw is None or quantidade_placas_raw <= 0) else quantidade_placas_raw
            )
            cur.execute(
                "INSERT INTO closed_deals (id, customer_id, valor_total_projeto, custo_projeto_total, lucro_venda, valor_entrada, valor_parcela, quantidade_parcelas, data_pagamento_entrada, inversor, quantidade_placas, observacao_custos, observacao, juros_percentual, valor_total_financiamento)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    closed_id,
                    cid,
                    valor_total,
                    custo_total,
                    lucro_venda,
                    valor_entrada,
                    valor_parcela,
                    quantidade_parcelas,
                    data_pagamento_entrada,
                    inversor,
                    quantidade_placas_val,
                    observacao_custos,
                    observacao,
                    juros_percentual,
                    valor_total_financiamento,
                ),
            )
            # Create parcelas
            for idx, date_str in enumerate(parcel_dates, start=1):
                parcel_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO parcelas (id, closed_deal_id, numero_parcela, data_pagamento, pago) VALUES (?, ?, ?, ?, ?)",
                    (parcel_id, closed_id, idx, date_str, 0)
                )
        # Process uploaded files: ORCAMENTO_FINAL and DOCUMENTO
        final_files = [f for f in files if f[0] == 'orcamento_final']
        document_files = [f for f in files if f[0] == 'documento_cliente']
        for file_type_label, file_list in [('ORCAMENTO_FINAL', final_files), ('DOCUMENTO', document_files)]:
            for _, file_info in file_list:
                file_id = str(uuid.uuid4())
                filename = file_info['filename']
                mime_type = file_info['content_type'] or 'application/octet-stream'
                data = file_info['data']
                size = len(data)
                safe_name = f"{file_id}_{filename}"
                path = os.path.join(UPLOAD_DIR, safe_name)
                with open(path, 'wb') as f:
                    f.write(data)
                cur.execute(
                    "INSERT INTO customer_files (id, customer_id, type, file_name, mime_type, size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (file_id, cid, file_type_label, filename, mime_type, size, datetime.utcnow().isoformat())
                )
                self.log_action(cid, 'UPLOAD_FILE', {'file_type': file_type_label, 'file_name': filename})
        # If the customer was not closed yet, update status
        if cust['status'] != 'FECHADO':
            cur.execute(
                "UPDATE customers SET status = 'FECHADO', updated_at = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), cid)
            )
        self.db_conn.commit()
        # Determine final quantity of panels for logging.  If the user provided a
        # new value >0 use it; otherwise use the value from the existing closed
        # deal or default to 1.
        if quantidade_placas_raw is not None and quantidade_placas_raw > 0:
            quantidade_placas_final = quantidade_placas_raw
        else:
            if existing_closed:
                quantidade_placas_final = existing_closed['quantidade_placas']
            else:
                quantidade_placas_final = 1
        # Audit log
        self.log_action(cid, 'CLOSE_CUSTOMER', {
            'valor_total_projeto': valor_total,
            'custo_projeto_total': custo_total,
            'lucro_venda': lucro_venda,
            'valor_entrada': valor_entrada,
            'valor_parcela': valor_parcela,
            'quantidade_parcelas': quantidade_parcelas,
            'data_pagamento_entrada': data_pagamento_entrada,
            'parcelas_datas': parcel_dates,
            'inversor': inversor,
            'quantidade_placas': quantidade_placas_final,
            'observacao_custos': observacao_custos,
            'observacao': observacao,
            'juros_percentual': juros_percentual,
            'valor_total_financiamento': valor_total_financiamento,
        })
        # Redirect back to customer detail page
        self.redirect(f'/customers/{cid}')

    def send_error_message_close(self, cid: str, message: str):
        """Helper to render the close form again with an error message."""
        html = render_template('customer_close.html', {
            'customer_id': cid,
            'error': message,
        })
        self.send_response(HTTPStatus.BAD_REQUEST)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def handle_file_serving(self, path: str):
        # path = /file/<file_id>
        parts = path.split('/')
        if len(parts) != 3:
            return self.send_error(HTTPStatus.NOT_FOUND, "Arquivo não encontrado")
        file_id = parts[2]
        cur = self.db_conn.cursor()
        f = cur.execute(
            "SELECT * FROM customer_files WHERE id = ?",
            (file_id,)
        ).fetchone()
        if not f:
            return self.send_error(HTTPStatus.NOT_FOUND, "Arquivo não encontrado")
        # Construct safe filename
        for item in os.listdir(UPLOAD_DIR):
            if item.startswith(f"{file_id}_"):
                file_path = os.path.join(UPLOAD_DIR, item)
                break
        else:
            return self.send_error(HTTPStatus.NOT_FOUND, "Arquivo não encontrado")
        # Serve file
        try:
            with open(file_path, 'rb') as fh:
                data = fh.read()
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type', f['mime_type'])
            self.send_header('Content-Disposition', f"attachment; filename={f['file_name']}")
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            return self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Erro ao ler arquivo")

    def log_action(self, customer_id: str, action: str, details: Dict):
        cur = self.db_conn.cursor()
        cur.execute(
            "INSERT INTO audit_logs (id, customer_id, user_id, action, details, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), customer_id, self.current_user or 'unknown', action, json.dumps(details), datetime.utcnow().isoformat())
        )
        self.db_conn.commit()


def run_server(port: int = 8000):
    init_db()
    handler_class = SoluxHandler
    with HTTPServer(("0.0.0.0", port), handler_class) as httpd:
        print(f"SOLUX server running on http://localhost:{port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("Shutting down.")


if __name__ == '__main__':
    run_server(int(os.environ.get('PORT', '8000')))
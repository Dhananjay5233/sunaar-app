"""
Sunaar - Multi-shop Gold Loan Management System (SaaS)
Roles:  superadmin (you)  ->  owner (shop owner)  ->  staff (shop employees)
Every shop's data is completely isolated from other shops.
"""
import io
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, date
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-key-in-production')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

app.config['SIGNUP_ENABLED'] = os.environ.get('SIGNUP_ENABLED', '1') != '0'
app.config['TRIAL_DAYS'] = int(os.environ.get('TRIAL_DAYS', '14'))
app.config['SUPPORT_CONTACT'] = os.environ.get('SUPPORT_CONTACT', '')  # e.g. "98765 43210" shown for renewals

DATABASE = os.environ.get('DATABASE_PATH', 'sunaar.db')
UPLOAD_DIR = os.environ.get('UPLOAD_DIR', 'uploads')
ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


# ============================================================ DATABASE
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS shops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        address TEXT,
        city TEXT,
        licence_no TEXT,
        valid_until DATE,
        is_active INTEGER DEFAULT 1,
        loan_seq INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id INTEGER,
        username TEXT UNIQUE NOT NULL,
        full_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('superadmin','owner','staff')),
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(shop_id) REFERENCES shops(id)
    );
    CREATE TABLE IF NOT EXISTS loans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id INTEGER NOT NULL,
        loan_no TEXT NOT NULL,
        customer_name TEXT NOT NULL,
        mobile TEXT NOT NULL,
        father_name TEXT,
        id_proof TEXT,
        address TEXT,
        item_desc TEXT NOT NULL,
        item_type TEXT,
        gross_weight REAL NOT NULL,
        net_weight REAL,
        purity TEXT,
        item_count INTEGER DEFAULT 1,
        photo TEXT,
        loan_amount REAL NOT NULL,
        interest_rate REAL NOT NULL,
        loan_date DATE NOT NULL,
        due_date DATE,
        notes TEXT,
        status TEXT DEFAULT 'active',
        closed_date DATE,
        closed_amount REAL,
        closed_by_name TEXT,
        created_by INTEGER NOT NULL,
        created_by_name TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(shop_id, loan_no),
        FOREIGN KEY(shop_id) REFERENCES shops(id),
        FOREIGN KEY(created_by) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id INTEGER NOT NULL,
        loan_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payment_date DATE NOT NULL,
        notes TEXT,
        received_by_name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(loan_id) REFERENCES loans(id)
    );
    CREATE INDEX IF NOT EXISTS idx_loans_shop_mobile ON loans(shop_id, mobile);
    CREATE INDEX IF NOT EXISTS idx_loans_shop_status ON loans(shop_id, status);
    CREATE INDEX IF NOT EXISTS idx_pay_loan ON payments(loan_id);
    ''')

    # light migration for databases created before sign-up existed
    shop_cols = [r[1] for r in c.execute('PRAGMA table_info(shops)')]
    if 'source' not in shop_cols:
        c.execute("ALTER TABLE shops ADD COLUMN source TEXT DEFAULT 'admin'")

    if c.execute("SELECT COUNT(*) FROM users WHERE role='superadmin'").fetchone()[0] == 0:
        pw = os.environ.get('SUPERADMIN_PASSWORD', 'super123')
        c.execute("INSERT INTO users (shop_id, username, full_name, password_hash, role) VALUES (NULL, ?, ?, ?, 'superadmin')",
                  ('superadmin', 'Super Admin', generate_password_hash(pw)))
        print(f"\n*** Super admin created -> username: superadmin  password: {pw}  (CHANGE IT!) ***\n")
    conn.commit()
    conn.close()


# ============================================================ HELPERS
def today():
    return date.today()


def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(16)
    return session['_csrf']


def allowed_file(name):
    return '.' in name and name.rsplit('.', 1)[1].lower() in ALLOWED_EXT


def fmt_money(v):
    """Indian digit grouping: 598000 -> 5,98,000"""
    try:
        n = int(round(float(v or 0)))
    except (TypeError, ValueError):
        return '0'
    sign, s = ('-' if n < 0 else ''), str(abs(n))
    if len(s) > 3:
        head, tail, groups = s[:-3], s[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ','.join(groups + [tail])
    return sign + s


def fmt_date(v):
    if not v:
        return '-'
    try:
        return datetime.strptime(str(v)[:10], '%Y-%m-%d').strftime('%d %b %Y')
    except ValueError:
        return str(v)


def calc_interest(loan):
    """Simple interest, per-month rate, pro-rata by days. `loan` must carry `paid`."""
    start = datetime.strptime(loan['loan_date'], '%Y-%m-%d').date()
    end = today()
    if loan['status'] == 'closed' and loan['closed_date']:
        end = datetime.strptime(loan['closed_date'], '%Y-%m-%d').date()
    days = max(1, (end - start).days)
    months = days / 30
    interest = loan['loan_amount'] * (loan['interest_rate'] / 100) * months
    paid = loan['paid'] if 'paid' in loan.keys() else 0
    total = loan['loan_amount'] + interest - (paid or 0)
    return {'days': days, 'months': round(months, 2), 'interest': round(interest),
            'paid': round(paid or 0), 'total': round(total), 'principal': loan['loan_amount']}


def is_overdue(loan):
    if loan['status'] != 'active' or not loan['due_date']:
        return False
    return datetime.strptime(loan['due_date'], '%Y-%m-%d').date() < today()


LOAN_SELECT = '''SELECT l.*, COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.loan_id = l.id), 0) AS paid
                 FROM loans l'''

def days_left(shop):
    if not shop or not shop['valid_until']:
        return None
    return (datetime.strptime(shop['valid_until'], '%Y-%m-%d').date() - today()).days


app.jinja_env.globals.update(calc_interest=calc_interest, is_overdue=is_overdue, days_left=days_left,
                             csrf_token=csrf_token, money=fmt_money, fdate=fmt_date, now=datetime.now)


# ============================================================ AUTH / TENANCY
@app.before_request
def load_user():
    g.user = None
    g.shop = None
    uid = session.get('user_id')
    if uid:
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
        shop = None
        if user and user['shop_id']:
            shop = conn.execute('SELECT * FROM shops WHERE id = ?', (user['shop_id'],)).fetchone()
        conn.close()
        ok = user and user['is_active']
        if ok and shop:
            ok = shop['is_active'] and (not shop['valid_until'] or shop['valid_until'] >= today().isoformat())
        if ok:
            g.user, g.shop = user, shop
        else:
            session.clear()

    if request.method == 'POST':
        sent = request.form.get('csrf_token', '')
        if not sent or sent != session.get('_csrf'):
            abort(400, 'Invalid or missing security token. Page refresh karke dobara try karein.')


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user:
            return redirect(url_for('login'))
        return f(*a, **kw)
    return wrapper


def shop_user_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if g.user['role'] == 'superadmin':
            return redirect(url_for('admin_home'))
        return f(*a, **kw)
    return wrapper


def owner_required(f):
    @wraps(f)
    @shop_user_required
    def wrapper(*a, **kw):
        if g.user['role'] != 'owner':
            flash('Ye kaam sirf shop owner kar sakta hai.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*a, **kw)
    return wrapper


def superadmin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if g.user['role'] != 'superadmin':
            abort(403)
        return f(*a, **kw)
    return wrapper


def get_loan_or_404(loan_id):
    conn = get_db()
    loan = conn.execute(LOAN_SELECT + ' WHERE l.id = ? AND l.shop_id = ?',
                        (loan_id, g.user['shop_id'])).fetchone()
    conn.close()
    if not loan:
        abort(404)
    return loan


@app.route('/login', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(url_for('dashboard') if g.user['role'] != 'superadmin' else url_for('admin_home'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE lower(username) = ?', (username,)).fetchone()
        shop = conn.execute('SELECT * FROM shops WHERE id = ?', (user['shop_id'],)).fetchone() if user and user['shop_id'] else None
        conn.close()
        if not user or not check_password_hash(user['password_hash'], password):
            flash('Galat username ya password.', 'danger')
        elif not user['is_active']:
            flash('Aapka account band hai. Shop owner se sampark karein.', 'danger')
        elif shop and not shop['is_active']:
            flash('Is shop ka account band hai. Support se sampark karein.', 'danger')
        elif shop and shop['valid_until'] and shop['valid_until'] < today().isoformat():
            contact = app.config['SUPPORT_CONTACT']
            flash('Aapka subscription expire ho gaya hai. Renew ke liye sampark karein' +
                  (f': {contact}' if contact else '.'), 'danger')
        else:
            session.clear()
            session.permanent = True
            session['user_id'] = user['id']
            return redirect(url_for('admin_home') if user['role'] == 'superadmin' else url_for('dashboard'))
    return render_template('login.html')


_signup_hits = {}   # ip -> [timestamps]; fine for the single-worker setup used here


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if g.user:
        return redirect(url_for('home'))
    if not app.config['SIGNUP_ENABLED']:
        abort(404)
    form = request.form
    if request.method == 'POST':
        if form.get('website'):                       # honeypot: real people never fill this hidden field
            return redirect(url_for('login'))
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
        stamp = datetime.now().timestamp()
        hits = [t for t in _signup_hits.get(ip, []) if stamp - t < 3600]
        if len(hits) >= 5:
            flash('Is network se bahut zyada account ban chuke hain. Thodi der baad koshish karein.', 'danger')
            return render_template('signup.html', form=form), 429

        shop_name = form.get('shop_name', '').strip()
        owner_name = form.get('owner_name', '').strip()
        phone = form.get('phone', '').strip()
        city = form.get('city', '').strip()
        username = form.get('username', '').strip().lower()
        password = form.get('password', '')
        error = None
        if not shop_name or not owner_name:
            error = 'Shop ka naam aur aapka naam zaroori hai.'
        elif not (phone.isdigit() and len(phone) == 10):
            error = 'Mobile number 10 digits ka hona chahiye.'
        elif len(username) < 3 or not all(ch.isalnum() or ch in '._' for ch in username):
            error = 'Username kam se kam 3 character ka ho, aur usme sirf letters, numbers, . aur _ hon.'
        elif len(password) < 6:
            error = 'Password kam se kam 6 character ka hona chahiye.'
        elif password != form.get('confirm_password', ''):
            error = 'Dono password match nahin kar rahe.'
        if error:
            flash(error, 'danger')
            return render_template('signup.html', form=form)

        valid_until = (today() + timedelta(days=app.config['TRIAL_DAYS'])).isoformat()
        conn = get_db()
        try:
            cur = conn.execute("INSERT INTO shops (name, phone, city, valid_until, source) VALUES (?,?,?,?, 'signup')",
                               (shop_name, phone, city, valid_until))
            ucur = conn.execute("INSERT INTO users (shop_id, username, full_name, password_hash, role) VALUES (?,?,?,?, 'owner')",
                                (cur.lastrowid, username, owner_name, generate_password_hash(password)))
            conn.commit()
            user_id = ucur.lastrowid
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.close()
            flash('Ye username pehle se kisi ne le liya hai. Doosra chunein.', 'danger')
            return render_template('signup.html', form=form)
        conn.close()

        hits.append(stamp)
        _signup_hits[ip] = hits
        session.clear()
        session.permanent = True
        session['user_id'] = user_id
        flash(f'Swagat hai! Aapka {app.config["TRIAL_DAYS"]} din ka free trial shuru ho gaya.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('signup.html', form={})


@app.route('/logout', methods=['POST', 'GET'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    if request.method == 'POST':
        old, new, confirm = (request.form.get(k, '') for k in ('old_password', 'new_password', 'confirm_password'))
        if new != confirm:
            flash('Naya password match nahin ho raha.', 'danger')
        elif len(new) < 6:
            flash('Password kam se kam 6 character ka hona chahiye.', 'danger')
        elif not check_password_hash(g.user['password_hash'], old):
            flash('Purana password galat hai.', 'danger')
        else:
            conn = get_db()
            conn.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                         (generate_password_hash(new), g.user['id']))
            conn.commit()
            conn.close()
            flash('Password badal gaya.', 'success')
            return redirect(url_for('account'))
    return render_template('account.html')


# ============================================================ DASHBOARD
@app.route('/')
@login_required
def home():
    return redirect(url_for('admin_home') if g.user['role'] == 'superadmin' else url_for('dashboard'))


@app.route('/dashboard')
@shop_user_required
def dashboard():
    sid = g.user['shop_id']
    is_owner = g.user['role'] == 'owner'
    scope_sql = '' if is_owner else ' AND l.created_by = :uid'
    params = {'sid': sid, 'uid': g.user['id']}
    conn = get_db()

    stats = conn.execute(f'''
        SELECT
          COALESCE(SUM(CASE WHEN status='active' THEN 1 END),0) AS active_count,
          COALESCE(SUM(CASE WHEN status='closed' THEN 1 END),0) AS closed_count,
          COALESCE(SUM(CASE WHEN status='active' THEN loan_amount END),0) AS active_amount,
          COALESCE(SUM(CASE WHEN status='active' THEN COALESCE(net_weight, gross_weight) END),0) AS gold_weight
        FROM loans l WHERE l.shop_id = :sid {scope_sql}''', params).fetchone()

    in30 = (today() + timedelta(days=30)).isoformat()
    due = conn.execute(LOAN_SELECT + f''' WHERE l.shop_id = :sid AND l.status='active'
                         AND l.due_date IS NOT NULL AND l.due_date <= :in30 {scope_sql}
                         ORDER BY l.due_date LIMIT 6''', {**params, 'in30': in30}).fetchall()
    recent = conn.execute(LOAN_SELECT + f' WHERE l.shop_id = :sid {scope_sql} ORDER BY l.id DESC LIMIT 6',
                          params).fetchall()

    six_ago = (today().replace(day=1) - timedelta(days=150)).replace(day=1).isoformat()
    monthly_rows = conn.execute(f'''SELECT substr(loan_date,1,7) AS m, SUM(loan_amount) AS amt, COUNT(*) AS n
                                    FROM loans l WHERE l.shop_id = :sid AND loan_date >= :six {scope_sql}
                                    GROUP BY m ORDER BY m''', {**params, 'six': six_ago}).fetchall()
    monthly = {r['m']: r for r in monthly_rows}
    months = []
    d = today().replace(day=1)
    for _ in range(6):
        months.append(d.strftime('%Y-%m'))
        d = (d - timedelta(days=1)).replace(day=1)
    months.reverse()
    chart = [{'label': datetime.strptime(m, '%Y-%m').strftime('%b'),
              'amt': monthly[m]['amt'] if m in monthly else 0,
              'n': monthly[m]['n'] if m in monthly else 0} for m in months]
    peak = max([c['amt'] for c in chart] + [1])
    for c in chart:
        c['pct'] = round(c['amt'] / peak * 100)

    staff_stats = []
    if is_owner:
        staff_stats = conn.execute('''
            SELECT u.id, u.full_name, u.role, u.is_active,
                   COUNT(l.id) AS total,
                   COALESCE(SUM(CASE WHEN l.status='active' THEN 1 END),0) AS active,
                   COALESCE(SUM(l.loan_amount),0) AS amount
            FROM users u LEFT JOIN loans l ON l.created_by = u.id
            WHERE u.shop_id = ? GROUP BY u.id ORDER BY amount DESC''', (sid,)).fetchall()
    conn.close()
    return render_template('dashboard.html', stats=stats, due=due, recent=recent, chart=chart,
                           staff_stats=staff_stats, is_owner=is_owner)


# ============================================================ LOANS
@app.route('/loans')
@shop_user_required
def all_loans():
    flt = request.args.get('filter', 'all')
    by = request.args.get('by', '')
    conn = get_db()
    where, params = ['l.shop_id = ?'], [g.user['shop_id']]
    if flt == 'active':
        where.append("l.status = 'active'")
    elif flt == 'closed':
        where.append("l.status = 'closed'")
    elif flt == 'overdue':
        where.append("l.status = 'active' AND l.due_date < ?")
        params.append(today().isoformat())
    if by == 'me':
        where.append('l.created_by = ?')
        params.append(g.user['id'])
    elif by.isdigit() and g.user['role'] == 'owner':
        where.append('l.created_by = ?')
        params.append(int(by))
    loans = conn.execute(LOAN_SELECT + ' WHERE ' + ' AND '.join(where) + ' ORDER BY l.id DESC', params).fetchall()
    team = conn.execute('SELECT id, full_name FROM users WHERE shop_id = ? ORDER BY full_name',
                        (g.user['shop_id'],)).fetchall() if g.user['role'] == 'owner' else []
    conn.close()
    return render_template('loans.html', loans=loans, flt=flt, by=by, team=team)


@app.route('/loan/new', methods=['GET', 'POST'])
@shop_user_required
def new_loan():
    if request.method == 'POST':
        f = request.form
        try:
            customer = f['customer_name'].strip()
            mobile = f['mobile'].strip()
            item_desc = f['item_desc'].strip()
            gross = float(f['gross_weight'])
            amount = float(f['loan_amount'])
            rate = float(f['interest_rate'])
            net = float(f.get('net_weight') or gross)
            count = int(f.get('item_count') or 1)
            if not (customer and item_desc) or gross <= 0 or amount <= 0 or rate < 0 or net <= 0 or count < 1:
                raise ValueError
        except (KeyError, ValueError):
            flash('Kripya sabhi zaroori fields sahi se bharein.', 'danger')
            return redirect(url_for('new_loan'))
        if not (mobile.isdigit() and len(mobile) == 10):
            flash('Mobile number 10 digits ka hona chahiye.', 'danger')
            return redirect(url_for('new_loan'))

        photo_name = None
        photo = request.files.get('photo')
        if photo and photo.filename:
            if not allowed_file(photo.filename):
                flash('Photo sirf png/jpg/jpeg/gif/webp ho sakti hai.', 'danger')
                return redirect(url_for('new_loan'))
            folder = os.path.join(UPLOAD_DIR, f"shop_{g.user['shop_id']}")
            os.makedirs(folder, exist_ok=True)
            photo_name = f"{secrets.token_hex(8)}.{photo.filename.rsplit('.', 1)[1].lower()}"
            photo.save(os.path.join(folder, photo_name))

        conn = get_db()
        conn.execute('UPDATE shops SET loan_seq = loan_seq + 1 WHERE id = ?', (g.user['shop_id'],))
        seq = conn.execute('SELECT loan_seq FROM shops WHERE id = ?', (g.user['shop_id'],)).fetchone()[0]
        loan_no = f'GL-{seq:05d}'
        cur = conn.execute('''INSERT INTO loans
            (shop_id, loan_no, customer_name, mobile, father_name, id_proof, address, item_desc, item_type,
             gross_weight, net_weight, purity, item_count, photo, loan_amount, interest_rate, loan_date,
             due_date, notes, created_by, created_by_name)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (g.user['shop_id'], loan_no, customer, mobile, f.get('father_name', '').strip(),
             f.get('id_proof', '').strip(), f.get('address', '').strip(), item_desc,
             f.get('item_type', 'Other'), gross, net, f.get('purity', '22K'), count, photo_name,
             amount, rate, f.get('loan_date') or today().isoformat(), f.get('due_date') or None,
             f.get('notes', '').strip(), g.user['id'], g.user['full_name']))
        conn.commit()
        new_id = cur.lastrowid
        conn.close()
        flash(f'Loan {loan_no} ban gaya.', 'success')
        return redirect(url_for('view_loan', loan_id=new_id))

    return render_template('new_loan.html', today=today().isoformat(),
                           default_due=(today() + timedelta(days=365)).isoformat())


@app.route('/loan/<int:loan_id>')
@shop_user_required
def view_loan(loan_id):
    loan = get_loan_or_404(loan_id)
    conn = get_db()
    payments = conn.execute('SELECT * FROM payments WHERE loan_id = ? AND shop_id = ? ORDER BY id DESC',
                            (loan_id, g.user['shop_id'])).fetchall()
    others = conn.execute('''SELECT COUNT(*) FROM loans WHERE shop_id = ? AND mobile = ? AND id != ?''',
                          (g.user['shop_id'], loan['mobile'], loan_id)).fetchone()[0]
    conn.close()
    return render_template('view_loan.html', loan=loan, interest=calc_interest(loan),
                           payments=payments, other_loans=others)


@app.route('/loan/<int:loan_id>/receipt')
@shop_user_required
def receipt(loan_id):
    loan = get_loan_or_404(loan_id)
    return render_template('receipt.html', loan=loan, interest=calc_interest(loan))


@app.route('/loan/<int:loan_id>/photo')
@shop_user_required
def loan_photo(loan_id):
    loan = get_loan_or_404(loan_id)
    if not loan['photo']:
        abort(404)
    return send_from_directory(os.path.abspath(os.path.join(UPLOAD_DIR, f"shop_{g.user['shop_id']}")), loan['photo'])


@app.route('/loan/<int:loan_id>/close', methods=['POST'])
@shop_user_required
def close_loan(loan_id):
    loan = get_loan_or_404(loan_id)
    if loan['status'] != 'active':
        flash('Ye loan pehle se close hai.', 'warning')
        return redirect(url_for('view_loan', loan_id=loan_id))
    try:
        amount = float(request.form['amount'])
        if amount < 0:
            raise ValueError
    except (KeyError, ValueError):
        flash('Amount sahi daalein.', 'danger')
        return redirect(url_for('view_loan', loan_id=loan_id))
    conn = get_db()
    conn.execute('''UPDATE loans SET status='closed', closed_amount=?, closed_date=?, closed_by_name=?
                    WHERE id=? AND shop_id=?''',
                 (amount, today().isoformat(), g.user['full_name'], loan_id, g.user['shop_id']))
    conn.commit()
    conn.close()
    flash(f'Loan close ho gaya (Rs {fmt_money(amount)}). Sona customer ko vapas de dein.', 'success')
    return redirect(url_for('view_loan', loan_id=loan_id))


@app.route('/loan/<int:loan_id>/payment', methods=['POST'])
@shop_user_required
def add_payment(loan_id):
    loan = get_loan_or_404(loan_id)
    if loan['status'] != 'active':
        flash('Closed loan par payment add nahin ho sakti.', 'warning')
        return redirect(url_for('view_loan', loan_id=loan_id))
    try:
        amount = float(request.form['amount'])
        if amount <= 0:
            raise ValueError
    except (KeyError, ValueError):
        flash('Amount sahi daalein.', 'danger')
        return redirect(url_for('view_loan', loan_id=loan_id))
    conn = get_db()
    conn.execute('''INSERT INTO payments (shop_id, loan_id, amount, payment_date, notes, received_by_name)
                    VALUES (?,?,?,?,?,?)''',
                 (g.user['shop_id'], loan_id, amount, today().isoformat(),
                  request.form.get('notes', '').strip(), g.user['full_name']))
    conn.commit()
    conn.close()
    flash(f'Payment Rs {fmt_money(amount)} record ho gayi.', 'success')
    return redirect(url_for('view_loan', loan_id=loan_id))


@app.route('/loan/<int:loan_id>/delete', methods=['POST'])
@owner_required
def delete_loan(loan_id):
    loan = get_loan_or_404(loan_id)
    conn = get_db()
    conn.execute('DELETE FROM payments WHERE loan_id = ? AND shop_id = ?', (loan_id, g.user['shop_id']))
    conn.execute('DELETE FROM loans WHERE id = ? AND shop_id = ?', (loan_id, g.user['shop_id']))
    conn.commit()
    conn.close()
    if loan['photo']:
        try:
            os.remove(os.path.join(UPLOAD_DIR, f"shop_{g.user['shop_id']}", loan['photo']))
        except OSError:
            pass
    flash('Loan delete ho gaya.', 'success')
    return redirect(url_for('all_loans'))


@app.route('/search')
@shop_user_required
def search():
    q = request.args.get('q', '').strip()
    results = []
    if q:
        like = f'%{q}%'
        conn = get_db()
        results = conn.execute(LOAN_SELECT + ''' WHERE l.shop_id = ? AND
                (l.mobile LIKE ? OR l.customer_name LIKE ? OR l.loan_no LIKE ?)
                ORDER BY l.id DESC LIMIT 100''', (g.user['shop_id'], like, like, like)).fetchall()
        conn.close()
    summary = None
    if results and q.isdigit() and len(q) == 10:
        act = [r for r in results if r['status'] == 'active']
        summary = {'name': results[0]['customer_name'], 'mobile': q, 'total': len(results),
                   'active': len(act), 'amount': sum(r['loan_amount'] for r in act),
                   'payable': sum(calc_interest(r)['total'] for r in act)}
    return render_template('search.html', results=results, q=q, summary=summary)


@app.route('/export')
@owner_required
def export_data():
    conn = get_db()
    sid = g.user['shop_id']
    data = {'shop': dict(g.shop),
            'loans': [dict(r) for r in conn.execute('SELECT * FROM loans WHERE shop_id = ?', (sid,))],
            'payments': [dict(r) for r in conn.execute('SELECT * FROM payments WHERE shop_id = ?', (sid,))],
            'exported_at': datetime.now().isoformat()}
    conn.close()
    buf = io.BytesIO(json.dumps(data, indent=2, default=str).encode())
    return send_file(buf, as_attachment=True, mimetype='application/json',
                     download_name=f'backup-{today().strftime("%Y%m%d")}.json')


# ============================================================ TEAM (owner)
@app.route('/team')
@owner_required
def team():
    conn = get_db()
    members = conn.execute('''SELECT u.*, COUNT(l.id) AS loans_created
                              FROM users u LEFT JOIN loans l ON l.created_by = u.id
                              WHERE u.shop_id = ? GROUP BY u.id ORDER BY u.role, u.full_name''',
                           (g.user['shop_id'],)).fetchall()
    conn.close()
    return render_template('team.html', members=members)


@app.route('/team/new', methods=['POST'])
@owner_required
def team_new():
    full_name = request.form.get('full_name', '').strip()
    username = request.form.get('username', '').strip().lower()
    password = request.form.get('password', '')
    if not full_name or not username or len(password) < 6:
        flash('Naam, username aur 6+ character ka password zaroori hai.', 'danger')
        return redirect(url_for('team'))
    if not username.replace('_', '').replace('.', '').isalnum():
        flash('Username mein sirf letters, numbers, . aur _ allowed hain.', 'danger')
        return redirect(url_for('team'))
    conn = get_db()
    try:
        conn.execute("INSERT INTO users (shop_id, username, full_name, password_hash, role) VALUES (?,?,?,?, 'staff')",
                     (g.user['shop_id'], username, full_name, generate_password_hash(password)))
        conn.commit()
        flash(f'Staff "{full_name}" add ho gaya. Login: {username}', 'success')
    except sqlite3.IntegrityError:
        flash('Ye username pehle se maujood hai. Doosra chunein.', 'danger')
    conn.close()
    return redirect(url_for('team'))


def _staff_of_my_shop(conn, uid):
    return conn.execute("SELECT * FROM users WHERE id = ? AND shop_id = ? AND role = 'staff'",
                        (uid, g.user['shop_id'])).fetchone()


@app.route('/team/<int:uid>/toggle', methods=['POST'])
@owner_required
def team_toggle(uid):
    conn = get_db()
    u = _staff_of_my_shop(conn, uid)
    if not u:
        conn.close()
        abort(404)
    conn.execute('UPDATE users SET is_active = ? WHERE id = ?', (0 if u['is_active'] else 1, uid))
    conn.commit()
    conn.close()
    flash('Staff status update ho gaya.', 'success')
    return redirect(url_for('team'))


@app.route('/team/<int:uid>/reset', methods=['POST'])
@owner_required
def team_reset(uid):
    pw = request.form.get('password', '')
    conn = get_db()
    u = _staff_of_my_shop(conn, uid)
    if not u:
        conn.close()
        abort(404)
    if len(pw) < 6:
        flash('Password kam se kam 6 character ka ho.', 'danger')
    else:
        conn.execute('UPDATE users SET password_hash = ? WHERE id = ?', (generate_password_hash(pw), uid))
        conn.commit()
        flash(f'{u["full_name"]} ka password badal gaya.', 'success')
    conn.close()
    return redirect(url_for('team'))


@app.route('/settings', methods=['GET', 'POST'])
@owner_required
def settings():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Shop ka naam zaroori hai.', 'danger')
            return redirect(url_for('settings'))
        conn = get_db()
        conn.execute('UPDATE shops SET name=?, phone=?, address=?, city=?, licence_no=? WHERE id=?',
                     (name, request.form.get('phone', '').strip(), request.form.get('address', '').strip(),
                      request.form.get('city', '').strip(), request.form.get('licence_no', '').strip(),
                      g.user['shop_id']))
        conn.commit()
        conn.close()
        flash('Shop details save ho gayi.', 'success')
        return redirect(url_for('settings'))
    return render_template('settings.html')


# ============================================================ SUPER ADMIN
@app.route('/admin')
@superadmin_required
def admin_home():
    conn = get_db()
    shops = conn.execute('''
        SELECT s.*,
          (SELECT full_name FROM users WHERE shop_id = s.id AND role='owner' LIMIT 1) AS owner_name,
          (SELECT username FROM users WHERE shop_id = s.id AND role='owner' LIMIT 1) AS owner_username,
          (SELECT COUNT(*) FROM users WHERE shop_id = s.id) AS users_count,
          (SELECT COUNT(*) FROM loans WHERE shop_id = s.id) AS loans_count,
          (SELECT COALESCE(SUM(loan_amount),0) FROM loans WHERE shop_id = s.id AND status='active') AS active_amount
        FROM shops s ORDER BY s.id DESC''').fetchall()
    conn.close()
    t = today().isoformat()
    soon = (today() + timedelta(days=15)).isoformat()
    totals = {'shops': len(shops),
              'live': sum(1 for s in shops if s['is_active'] and (not s['valid_until'] or s['valid_until'] >= t)),
              'expiring': sum(1 for s in shops if s['valid_until'] and t <= s['valid_until'] <= soon),
              'loans': sum(s['loans_count'] for s in shops)}
    return render_template('admin_home.html', shops=shops, totals=totals, today=t, soon=soon)


@app.route('/admin/shop/new', methods=['GET', 'POST'])
@superadmin_required
def admin_new_shop():
    if request.method == 'POST':
        f = request.form
        name, owner = f.get('shop_name', '').strip(), f.get('owner_name', '').strip()
        username, pw = f.get('username', '').strip().lower(), f.get('password', '')
        try:
            months = int(f.get('months') or 12)
        except ValueError:
            months = 12
        if not (name and owner and username) or len(pw) < 6:
            flash('Shop naam, owner naam, username aur 6+ character password zaroori hai.', 'danger')
            return render_template('admin_new_shop.html', form=f)
        valid = (today() + timedelta(days=30 * months)).isoformat()
        conn = get_db()
        try:
            cur = conn.execute('INSERT INTO shops (name, phone, city, valid_until) VALUES (?,?,?,?)',
                               (name, f.get('phone', '').strip(), f.get('city', '').strip(), valid))
            conn.execute("INSERT INTO users (shop_id, username, full_name, password_hash, role) VALUES (?,?,?,?, 'owner')",
                         (cur.lastrowid, username, owner, generate_password_hash(pw)))
            conn.commit()
            flash(f'Shop "{name}" ban gayi. Owner login: {username}', 'success')
            conn.close()
            return redirect(url_for('admin_home'))
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.close()
            flash('Ye username pehle se maujood hai.', 'danger')
            return render_template('admin_new_shop.html', form=f)
    return render_template('admin_new_shop.html', form={})


@app.route('/admin/shop/<int:sid>/toggle', methods=['POST'])
@superadmin_required
def admin_toggle_shop(sid):
    conn = get_db()
    s = conn.execute('SELECT * FROM shops WHERE id = ?', (sid,)).fetchone()
    if not s:
        conn.close()
        abort(404)
    conn.execute('UPDATE shops SET is_active = ? WHERE id = ?', (0 if s['is_active'] else 1, sid))
    conn.commit()
    conn.close()
    flash('Shop status update ho gaya.', 'success')
    return redirect(url_for('admin_home'))


@app.route('/admin/shop/<int:sid>/extend', methods=['POST'])
@superadmin_required
def admin_extend_shop(sid):
    try:
        months = int(request.form.get('months', 12))
    except ValueError:
        months = 12
    conn = get_db()
    s = conn.execute('SELECT * FROM shops WHERE id = ?', (sid,)).fetchone()
    if not s:
        conn.close()
        abort(404)
    base = max(today(), datetime.strptime(s['valid_until'], '%Y-%m-%d').date()) if s['valid_until'] else today()
    new_valid = (base + timedelta(days=30 * months)).isoformat()
    conn.execute('UPDATE shops SET valid_until = ? WHERE id = ?', (new_valid, sid))
    conn.commit()
    conn.close()
    flash(f'Subscription {fmt_date(new_valid)} tak badh gaya.', 'success')
    return redirect(url_for('admin_home'))


@app.route('/admin/shop/<int:sid>/reset-owner', methods=['POST'])
@superadmin_required
def admin_reset_owner(sid):
    pw = request.form.get('password', '')
    if len(pw) < 6:
        flash('Password kam se kam 6 character ka ho.', 'danger')
        return redirect(url_for('admin_home'))
    conn = get_db()
    conn.execute("UPDATE users SET password_hash = ? WHERE shop_id = ? AND role = 'owner'",
                 (generate_password_hash(pw), sid))
    conn.commit()
    conn.close()
    flash('Owner ka password badal gaya.', 'success')
    return redirect(url_for('admin_home'))


@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, msg='Ye page ya record nahin mila.'), 404


@app.errorhandler(403)
def forbidden(e):
    return render_template('error.html', code=403, msg='Aapko is page ki permission nahin hai.'), 403


@app.errorhandler(400)
def bad_request(e):
    return render_template('error.html', code=400, msg=getattr(e, 'description', 'Bad request')), 400


@app.errorhandler(413)
def too_large(e):
    return render_template('error.html', code=413, msg='File 5MB se badi hai.'), 413


init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5003)),
            debug=os.environ.get('FLASK_DEBUG', '0') == '1')

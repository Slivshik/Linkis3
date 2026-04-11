import os, json, sqlite3, hashlib, secrets
from datetime import datetime, timedelta
from flask import g

DB_PATH = os.path.join(os.path.dirname(__file__), 'users.db')

def get_db():
    try:
        from flask import g
        if 'db' not in g:
            g.db = sqlite3.connect(DB_PATH)
            g.db.row_factory = sqlite3.Row
        return g.db
    except RuntimeError:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def hash_password(p): return hashlib.sha256(p.encode()).hexdigest()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE,
        password_hash TEXT,
        api_key TEXT UNIQUE,
        is_admin INTEGER DEFAULT 0,
        is_suspended INTEGER DEFAULT 0,
        suspend_reason TEXT,
        bio TEXT,
        avatar_emoji TEXT DEFAULT '🤖',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        invite_code TEXT,
        invited_by INTEGER,
        FOREIGN KEY (invited_by) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS usage_limits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL UNIQUE,
        daily_requests INTEGER DEFAULT 100,
        daily_tokens INTEGER DEFAULT 50000,
        requests_today INTEGER DEFAULT 0,
        tokens_today INTEGER DEFAULT 0,
        last_reset DATE DEFAULT CURRENT_DATE,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS api_keys_pool (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key_value TEXT UNIQUE NOT NULL,
        label TEXT,
        is_active INTEGER DEFAULT 1,
        requests_today INTEGER DEFAULT 0,
        total_requests INTEGER DEFAULT 0,
        last_used TIMESTAMP,
        error_count INTEGER DEFAULT 0,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        description TEXT,
        commands TEXT NOT NULL,
        category TEXT DEFAULT 'general',
        creator_id INTEGER,
        is_public INTEGER DEFAULT 0,
        usage_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (creator_id) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS invitations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invite_code TEXT UNIQUE NOT NULL,
        created_by INTEGER NOT NULL,
        max_uses INTEGER DEFAULT 1,
        current_uses INTEGER DEFAULT 0,
        expires_at TIMESTAMP,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (created_by) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS request_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        api_key_used TEXT,
        model TEXT,
        tokens_used INTEGER,
        success INTEGER,
        error_message TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        conversation_id TEXT NOT NULL,
        title TEXT,
        model TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, conversation_id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS conversation_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        model TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS announcements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_by INTEGER,
        content TEXT NOT NULL,
        type TEXT DEFAULT 'info',
        expires_at TIMESTAMP,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (created_by) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS user_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT DEFAULT 'Note',
        content TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    # Default admin
    c.execute('SELECT COUNT(*) FROM users WHERE is_admin=1')
    if c.fetchone()[0] == 0:
        admin_key = secrets.token_urlsafe(32)
        c.execute('INSERT INTO users (username,api_key,is_admin) VALUES (?,?,1)', ('admin', admin_key))
        uid = c.lastrowid
        c.execute('INSERT INTO usage_limits (user_id,daily_requests,daily_tokens) VALUES (?,999999,9999999)', (uid,))
        print(f"\n{'='*60}\nADMIN API KEY: {admin_key}\n{'='*60}\n")

    # Env keys
    for key in os.getenv('OPENROUTER_API_KEYS','').split(','):
        key = key.strip()
        if key: c.execute('INSERT OR IGNORE INTO api_keys_pool (key_value,label) VALUES (?,?)', (key,'Environment Key'))

    conn.commit(); conn.close()
    print("Database initialized")

# --- Auth ---
def get_user_by_api_key(api_key):
    db = get_db()
    row = db.execute('SELECT * FROM users WHERE api_key=?', (api_key,)).fetchone()
    return dict(row) if row else None

def get_user_by_credentials(username, password):
    db = get_db()
    ph = hash_password(password)
    row = db.execute('SELECT * FROM users WHERE username=? AND password_hash=?', (username, ph)).fetchone()
    if row:
        user = dict(row)
        if user.get('is_suspended'): return None
        db.execute("UPDATE users SET last_active=CURRENT_TIMESTAMP WHERE id=?", (user['id'],))
        db.commit()
        return user
    return None

def create_user(username, email=None, password=None, invite_code=None, invited_by=None):
    db = get_db()
    api_key = f"lk_{secrets.token_urlsafe(32)}"
    ph = hash_password(password) if password else None
    try:
        db.execute('INSERT INTO users (username,email,password_hash,api_key,invite_code,invited_by) VALUES (?,?,?,?,?,?)',
                   (username, email or None, ph, api_key, invite_code, invited_by))
        uid = db.execute('SELECT id FROM users WHERE api_key=?', (api_key,)).fetchone()[0]
        db.execute('INSERT OR IGNORE INTO usage_limits (user_id) VALUES (?)', (uid,))
        db.commit()
        return {'success': True, 'user_id': uid, 'api_key': api_key}
    except sqlite3.IntegrityError as e:
        return {'success': False, 'error': 'Username or email already taken'}

def update_user_profile(user_id, data):
    db = get_db()
    allowed = {'email', 'bio', 'avatar_emoji'}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates: return {'error': 'No valid fields'}
    sets = ', '.join(f"{k}=?" for k in updates)
    db.execute(f"UPDATE users SET {sets} WHERE id=?", (*updates.values(), user_id))
    db.commit()
    return {'success': True}

# --- Usage ---
def check_usage_limit(user_id):
    db = get_db()
    today = datetime.now().date()
    db.execute("UPDATE usage_limits SET requests_today=0,tokens_today=0,last_reset=? WHERE last_reset<?  AND user_id=?",
               (today, today, user_id))
    db.commit()
    row = db.execute("SELECT ul.*, u.is_admin FROM usage_limits ul JOIN users u ON ul.user_id=u.id WHERE ul.user_id=?", (user_id,)).fetchone()
    if not row: return {'allowed': True}
    r = dict(row)
    if r['is_admin']: return {'allowed': True}
    if r['is_suspended'] if 'is_suspended' in r else False: return {'allowed': False, 'reason': 'Account suspended'}
    if r['requests_today'] >= r['daily_requests']: return {'allowed': False, 'reason': 'Daily request limit reached', 'limit': r['daily_requests'], 'used': r['requests_today']}
    if r['tokens_today'] >= r['daily_tokens']: return {'allowed': False, 'reason': 'Daily token limit reached', 'limit': r['daily_tokens'], 'used': r['tokens_today']}
    return {'allowed': True, 'remaining_requests': r['daily_requests'] - r['requests_today']}

def increment_usage(user_id, tokens=0):
    db = get_db()
    db.execute("UPDATE usage_limits SET requests_today=requests_today+1, tokens_today=tokens_today+? WHERE user_id=?", (tokens, user_id))
    db.execute("UPDATE users SET last_active=CURRENT_TIMESTAMP WHERE id=?", (user_id,))
    db.commit()

# --- API Key Pool ---
def get_next_api_key():
    db = get_db()
    row = db.execute("SELECT * FROM api_keys_pool WHERE is_active=1 ORDER BY error_count ASC, requests_today ASC LIMIT 1").fetchone()
    if row:
        r = dict(row)
        db.execute("UPDATE api_keys_pool SET requests_today=requests_today+1,total_requests=total_requests+1,last_used=CURRENT_TIMESTAMP WHERE id=?", (r['id'],))
        db.commit()
        return r['key_value']
    return os.getenv('OPENROUTER_API_KEY')

def mark_api_key_error(key):
    db = get_db()
    db.execute("UPDATE api_keys_pool SET error_count=error_count+1 WHERE key_value=?", (key,))
    db.execute("UPDATE api_keys_pool SET is_active=0 WHERE key_value=? AND error_count>10", (key,))
    db.commit()

def reset_api_key_stats(key):
    db = get_db()
    db.execute("UPDATE api_keys_pool SET error_count=MAX(0,error_count-1) WHERE key_value=?", (key,))
    db.commit()

def add_api_key(key_value, label=None):
    db = get_db()
    try:
        db.execute("INSERT INTO api_keys_pool (key_value,label) VALUES (?,?)", (key_value, label))
        db.commit(); return {'success': True}
    except sqlite3.IntegrityError: return {'success': False, 'error': 'Key already exists'}

def remove_api_key(key_value):
    db = get_db()
    db.execute("DELETE FROM api_keys_pool WHERE key_value=?", (key_value,))
    db.commit(); return {'success': True}

def get_api_keys_pool():
    db = get_db()
    rows = db.execute("SELECT id,label,is_active,requests_today,total_requests,error_count,last_used,added_at,substr(key_value,1,8)||'...'||substr(key_value,-4) as masked_key FROM api_keys_pool ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]

def update_api_key_label(key_id, label): pass
def toggle_api_key(key_id, active): pass

# --- Skills ---
def create_skill(name, description, commands, category='general', creator_id=None, is_public=0):
    db = get_db()
    try:
        db.execute("INSERT INTO skills (name,description,commands,category,creator_id,is_public) VALUES (?,?,?,?,?,?)",
                   (name, description, json.dumps(commands) if isinstance(commands, list) else commands, category, creator_id, is_public))
        db.commit()
        row = db.execute("SELECT id FROM skills WHERE name=?", (name,)).fetchone()
        return {'success': True, 'skill_id': row[0]}
    except sqlite3.IntegrityError: return {'success': False, 'error': 'Skill already exists'}

def get_skill(name):
    db = get_db()
    row = db.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
    if row:
        db.execute("UPDATE skills SET usage_count=usage_count+1 WHERE name=?", (name,)); db.commit()
        return dict(row)
    return None

def get_all_skills(category=None, public_only=False):
    db = get_db()
    q = "SELECT s.*, u.username as creator_name FROM skills s LEFT JOIN users u ON s.creator_id=u.id"
    params = []
    conds = []
    if category: conds.append("s.category=?"); params.append(category)
    if public_only: conds.append("s.is_public=1")
    if conds: q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY s.usage_count DESC"
    return [dict(r) for r in db.execute(q, params).fetchall()]

def delete_skill(skill_id):
    db = get_db()
    db.execute("DELETE FROM skills WHERE id=?", (skill_id,)); db.commit()
    return {'success': True}

# --- Invitations ---
def create_invite(created_by, max_uses=1, expires_in_days=7):
    db = get_db()
    code = secrets.token_urlsafe(16)
    exp = datetime.now() + timedelta(days=expires_in_days)
    db.execute("INSERT INTO invitations (invite_code,created_by,max_uses,expires_at) VALUES (?,?,?,?)", (code, created_by, max_uses, exp))
    db.commit()
    return {'success': True, 'invite_code': code, 'expires_at': exp.isoformat()}

def validate_invite(code):
    db = get_db()
    row = db.execute("SELECT * FROM invitations WHERE invite_code=? AND is_active=1 AND current_uses<max_uses AND (expires_at IS NULL OR expires_at>?)", (code, datetime.now())).fetchone()
    return {'valid': True, 'invite': dict(row)} if row else {'valid': False, 'error': 'Invalid or expired invite'}

def use_invite(code):
    db = get_db()
    db.execute("UPDATE invitations SET current_uses=current_uses+1 WHERE invite_code=?", (code,))
    db.execute("UPDATE invitations SET is_active=0 WHERE invite_code=? AND current_uses>=max_uses", (code,))
    db.commit()

def get_user_invites(user_id):
    db = get_db()
    rows = db.execute("SELECT * FROM invitations WHERE created_by=? ORDER BY created_at DESC", (user_id,)).fetchall()
    return [dict(r) for r in rows]

# --- Logging ---
def log_request(user_id, api_key_used, model, tokens_used, success, error_message=None):
    db = get_db()
    db.execute("INSERT INTO request_logs (user_id,api_key_used,model,tokens_used,success,error_message) VALUES (?,?,?,?,?,?)",
               (user_id, api_key_used[:8] if api_key_used else None, model, tokens_used, 1 if success else 0, error_message))
    db.commit()

def get_user_stats(user_id):
    db = get_db()
    row = db.execute("SELECT COUNT(*) as total, SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as ok, SUM(tokens_used) as tokens FROM request_logs WHERE user_id=?", (user_id,)).fetchone()
    stats = dict(row) if row else {}
    stats['total_requests'] = stats.pop('total', 0) or 0
    stats['successful_requests'] = stats.pop('ok', 0) or 0
    stats['total_tokens'] = stats.pop('tokens', 0) or 0
    ul = db.execute("SELECT requests_today,tokens_today,daily_requests,daily_tokens FROM usage_limits WHERE user_id=?", (user_id,)).fetchone()
    if ul:
        stats['requests_today'] = ul[0]; stats['tokens_today'] = ul[1]
        stats['daily_request_limit'] = ul[2]; stats['daily_token_limit'] = ul[3]
    return stats

def get_request_logs(limit=100, user_id=None, success=None):
    db = get_db()
    q = "SELECT rl.*, u.username FROM request_logs rl LEFT JOIN users u ON rl.user_id=u.id"
    params = []; conds = []
    if user_id: conds.append("rl.user_id=?"); params.append(user_id)
    if success is not None: conds.append("rl.success=?"); params.append(1 if success=='true' else 0)
    if conds: q += " WHERE " + " AND ".join(conds)
    q += f" ORDER BY rl.created_at DESC LIMIT {limit}"
    rows = db.execute(q, params).fetchall()
    return [dict(r) for r in rows]

# --- Conversations ---
def save_conversation(user_id, conversation_id, user_msg, ai_msg, model):
    db = get_db()
    # Upsert conversation header
    row = db.execute("SELECT id FROM conversations WHERE user_id=? AND conversation_id=?", (user_id, conversation_id)).fetchone()
    title = user_msg[:60] + ('...' if len(user_msg) > 60 else '')
    if row:
        db.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND conversation_id=?", (user_id, conversation_id))
    else:
        db.execute("INSERT INTO conversations (user_id,conversation_id,title,model) VALUES (?,?,?,?)", (user_id, conversation_id, title, model))
    # Save messages
    db.execute("INSERT INTO conversation_messages (user_id,conversation_id,role,content,model) VALUES (?,?,?,?,?)", (user_id, conversation_id, 'user', user_msg, model))
    db.execute("INSERT INTO conversation_messages (user_id,conversation_id,role,content,model) VALUES (?,?,?,?,?)", (user_id, conversation_id, 'assistant', ai_msg, model))
    db.commit()

def get_conversations(user_id):
    db = get_db()
    rows = db.execute("SELECT * FROM conversations WHERE user_id=? ORDER BY updated_at DESC LIMIT 50", (user_id,)).fetchall()
    return [dict(r) for r in rows]

def get_conversation_messages(user_id, conversation_id):
    db = get_db()
    rows = db.execute("SELECT * FROM conversation_messages WHERE user_id=? AND conversation_id=? ORDER BY created_at ASC", (user_id, conversation_id)).fetchall()
    return [dict(r) for r in rows]

def delete_conversation(user_id, conversation_id):
    db = get_db()
    db.execute("DELETE FROM conversations WHERE user_id=? AND conversation_id=?", (user_id, conversation_id))
    db.execute("DELETE FROM conversation_messages WHERE user_id=? AND conversation_id=?", (user_id, conversation_id))
    db.commit()

# --- Admin ---
def get_all_users(search='', page=1, per_page=25):
    db = get_db()
    offset = (page - 1) * per_page
    q = "SELECT u.*, ul.requests_today, ul.tokens_today, ul.daily_requests, ul.daily_tokens FROM users u LEFT JOIN usage_limits ul ON u.id=ul.user_id"
    params = []
    if search:
        q += " WHERE (u.username LIKE ? OR u.email LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%'])
    total_row = db.execute(f"SELECT COUNT(*) FROM users u" + (" WHERE (u.username LIKE ? OR u.email LIKE ?)" if search else ""),
                           params[:2] if search else []).fetchone()
    total = total_row[0]
    q += f" ORDER BY u.created_at DESC LIMIT {per_page} OFFSET {offset}"
    rows = db.execute(q, params).fetchall()
    users = []
    for r in rows:
        u = dict(r); u.pop('password_hash', None); users.append(u)
    return {'users': users, 'total': total, 'page': page, 'per_page': per_page, 'pages': (total + per_page - 1) // per_page}

def update_user_limits(user_id, data):
    db = get_db()
    if 'is_admin' in data:
        db.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if data['is_admin'] else 0, user_id))
    if 'daily_requests' in data or 'daily_tokens' in data:
        ul = db.execute("SELECT id FROM usage_limits WHERE user_id=?", (user_id,)).fetchone()
        if not ul:
            db.execute("INSERT INTO usage_limits (user_id) VALUES (?)", (user_id,))
        if 'daily_requests' in data:
            db.execute("UPDATE usage_limits SET daily_requests=? WHERE user_id=?", (data['daily_requests'], user_id))
        if 'daily_tokens' in data:
            db.execute("UPDATE usage_limits SET daily_tokens=? WHERE user_id=?", (data['daily_tokens'], user_id))
    db.commit()
    return {'success': True}

def suspend_user(user_id, suspended=True, reason=''):
    db = get_db()
    db.execute("UPDATE users SET is_suspended=?, suspend_reason=? WHERE id=?", (1 if suspended else 0, reason, user_id))
    db.commit()
    return {'success': True}

def get_system_stats():
    db = get_db()
    total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active_24h = db.execute("SELECT COUNT(*) FROM users WHERE last_active > datetime('now','-1 day')").fetchone()[0]
    new_today = db.execute("SELECT COUNT(*) FROM users WHERE created_at > datetime('now','start of day')").fetchone()[0]
    total_requests = db.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0]
    requests_today = db.execute("SELECT COUNT(*) FROM request_logs WHERE created_at > datetime('now','start of day')").fetchone()[0]
    tokens_today = db.execute("SELECT SUM(tokens_used) FROM request_logs WHERE created_at > datetime('now','start of day')").fetchone()[0] or 0
    active_keys = db.execute("SELECT COUNT(*) FROM api_keys_pool WHERE is_active=1").fetchone()[0]
    total_convos = db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    total_skills = db.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    active_invites = db.execute("SELECT COUNT(*) FROM invitations WHERE is_active=1 AND current_uses<max_uses AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)").fetchone()[0]
    suspended_users = db.execute("SELECT COUNT(*) FROM users WHERE is_suspended=1").fetchone()[0]
    model_dist = [{'model':r[0],'count':r[1]} for r in db.execute("SELECT model, COUNT(*) as c FROM request_logs WHERE created_at > datetime('now','-7 days') GROUP BY model ORDER BY c DESC LIMIT 5").fetchall()]
    daily_reqs = [{'date':r[0],'count':r[1]} for r in db.execute("SELECT DATE(created_at) as d, COUNT(*) FROM request_logs GROUP BY d ORDER BY d DESC LIMIT 14").fetchall()]
    return {
        'total_users': total_users, 'active_24h': active_24h, 'new_today': new_today,
        'suspended_users': suspended_users, 'total_requests': total_requests,
        'requests_today': requests_today, 'tokens_today': tokens_today,
        'active_keys': active_keys, 'total_conversations': total_convos,
        'total_skills': total_skills, 'active_invites': active_invites,
        'model_distribution': model_dist, 'daily_requests': daily_reqs,
    }

# --- Announcements ---
def add_announcement(created_by, content, ann_type='info', expires_in_days=7):
    db = get_db()
    exp = datetime.now() + timedelta(days=expires_in_days)
    db.execute("INSERT INTO announcements (created_by,content,type,expires_at) VALUES (?,?,?,?)", (created_by, content, ann_type, exp))
    db.commit(); return {'success': True}

def get_announcements(include_expired=False):
    db = get_db()
    q = "SELECT * FROM announcements WHERE is_active=1"
    if not include_expired: q += " AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)"
    q += " ORDER BY created_at DESC"
    return [dict(r) for r in db.execute(q).fetchall()]

def delete_announcement(ann_id):
    db = get_db()
    db.execute("UPDATE announcements SET is_active=0 WHERE id=?", (ann_id,)); db.commit()
    return {'success': True}

# --- User Notes ---
def create_user_note(user_id, title, content):
    db = get_db()
    db.execute("INSERT INTO user_notes (user_id,title,content) VALUES (?,?,?)", (user_id, title, content))
    db.commit(); return {'success': True}

def get_user_notes(user_id):
    db = get_db()
    rows = db.execute("SELECT * FROM user_notes WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    return [dict(r) for r in rows]

def delete_user_note(user_id, note_id):
    db = get_db()
    db.execute("DELETE FROM user_notes WHERE id=? AND user_id=?", (note_id, user_id)); db.commit()
    return {'success': True}

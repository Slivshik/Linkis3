import os
import json
import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps
from flask import request, jsonify, g

DB_PATH = os.path.join(os.path.dirname(__file__), 'users.db')

def get_db():
    """Get database connection"""
    try:
        from flask import g
        if 'db' not in g:
            g.db = sqlite3.connect(DB_PATH)
            g.db.row_factory = sqlite3.Row
        return g.db
    except RuntimeError:
        # Outside Flask context, create direct connection
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    """Initialize the database with required tables"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE,
            password_hash TEXT,
            api_key TEXT UNIQUE,
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            invite_code TEXT,
            invited_by INTEGER,
            FOREIGN KEY (invited_by) REFERENCES users(id)
        )
    ''')
    
    # Usage limits table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usage_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            daily_requests INTEGER DEFAULT 100,
            daily_tokens INTEGER DEFAULT 50000,
            max_file_size INTEGER DEFAULT 1048576,
            max_files_per_request INTEGER DEFAULT 10,
            requests_today INTEGER DEFAULT 0,
            tokens_today INTEGER DEFAULT 0,
            last_reset DATE DEFAULT CURRENT_DATE,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    
    # API Keys pool table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS api_keys_pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_value TEXT UNIQUE NOT NULL,
            label TEXT,
            is_active INTEGER DEFAULT 1,
            requests_today INTEGER DEFAULT 0,
            total_requests INTEGER DEFAULT 0,
            last_used TIMESTAMP,
            error_count INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Skills table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            commands TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            created_by INTEGER,
            is_public INTEGER DEFAULT 0,
            usage_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    ''')
    
    # Invitations table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS invitations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invite_code TEXT UNIQUE NOT NULL,
            created_by INTEGER NOT NULL,
            max_uses INTEGER DEFAULT 1,
            current_uses INTEGER DEFAULT 0,
            expires_at TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    ''')
    
    # Request logs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            api_key_used TEXT,
            model TEXT,
            tokens_used INTEGER,
            success INTEGER,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create default admin user if not exists
    cursor.execute('SELECT COUNT(*) FROM users WHERE is_admin = 1')
    if cursor.fetchone()[0] == 0:
        admin_api_key = secrets.token_urlsafe(32)
        cursor.execute('''
            INSERT INTO users (username, api_key, is_admin)
            VALUES (?, ?, 1)
        ''', ('admin', admin_api_key))
        
        cursor.execute('''
            INSERT INTO usage_limits (user_id, daily_requests, daily_tokens)
            VALUES (?, 999999, 9999999)
        ''', (cursor.lastrowid,))
        
        print(f"\n{'='*50}")
        print(f"ADMIN API KEY (save this!): {admin_api_key}")
        print(f"{'='*50}\n")
    
    # Insert default API keys from environment
    env_api_keys = os.getenv('OPENROUTER_API_KEYS', '')
    if env_api_keys:
        for key in env_api_keys.split(','):
            key = key.strip()
            if key:
                cursor.execute('''
                    INSERT OR IGNORE INTO api_keys_pool (key_value, label)
                    VALUES (?, ?)
                ''', (key, 'Environment Key'))
    
    conn.commit()
    conn.close()
    print("Database initialized successfully")

def close_db(e=None):
    """Close database connection"""
    db = g.pop('db', None)
    if db is not None:
        db.close()

def hash_password(password):
    """Hash a password"""
    return hashlib.sha256(password.encode()).hexdigest()

def create_user(username, email=None, password=None, invite_code=None, invited_by=None):
    """Create a new user"""
    conn = get_db()
    cursor = conn.cursor()
    
    api_key = secrets.token_urlsafe(32)
    password_hash = hash_password(password) if password else None
    
    try:
        cursor.execute('''
            INSERT INTO users (username, email, password_hash, api_key, invite_code, invited_by)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (username, email, password_hash, api_key, invite_code, invited_by))
        
        user_id = cursor.lastrowid
        
        cursor.execute('''
            INSERT INTO usage_limits (user_id)
            VALUES (?)
        ''', (user_id,))
        
        conn.commit()
        return {'success': True, 'user_id': user_id, 'api_key': api_key}
    except sqlite3.IntegrityError as e:
        return {'success': False, 'error': str(e)}

def get_user_by_api_key(api_key):
    """Get user by API key"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM users WHERE api_key = ?', (api_key,))
    row = cursor.fetchone()
    
    if row:
        return dict(row)
    return None

def check_usage_limit(user_id):
    """Check if user has reached their usage limit"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Reset daily counters if it's a new day
    today = datetime.now().date()
    cursor.execute('''
        UPDATE usage_limits 
        SET requests_today = 0, tokens_today = 0, last_reset = ?
        WHERE last_reset < ? AND user_id = ?
    ''', (today, today, user_id))
    conn.commit()
    
    # Get current usage
    cursor.execute('''
        SELECT ul.*, u.is_admin 
        FROM usage_limits ul
        JOIN users u ON ul.user_id = u.id
        WHERE ul.user_id = ?
    ''', (user_id,))
    
    row = cursor.fetchone()
    if not row:
        return {'allowed': True, 'reason': 'No limits set'}
    
    usage = dict(row)
    
    # Admin users have no limits
    if usage['is_admin']:
        return {'allowed': True, 'reason': 'Admin user'}
    
    # Check request limit
    if usage['requests_today'] >= usage['daily_requests']:
        return {
            'allowed': False, 
            'reason': 'Daily request limit reached',
            'limit': usage['daily_requests'],
            'used': usage['requests_today']
        }
    
    # Check token limit
    if usage['tokens_today'] >= usage['daily_tokens']:
        return {
            'allowed': False, 
            'reason': 'Daily token limit reached',
            'limit': usage['daily_tokens'],
            'used': usage['tokens_today']
        }
    
    return {'allowed': True, 'remaining_requests': usage['daily_requests'] - usage['requests_today']}

def increment_usage(user_id, tokens_used=0):
    """Increment user's usage counters"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE usage_limits 
        SET requests_today = requests_today + 1, tokens_today = tokens_today + ?
        WHERE user_id = ?
    ''', (tokens_used, user_id))
    
    conn.commit()

def get_next_api_key():
    """Get the next available API key from the pool with load balancing"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Get active keys ordered by least used and lowest error count
    cursor.execute('''
        SELECT * FROM api_keys_pool 
        WHERE is_active = 1 
        ORDER BY error_count ASC, requests_today ASC, total_requests ASC
        LIMIT 1
    ''')
    
    row = cursor.fetchone()
    if row:
        key_data = dict(row)
        
        # Update last used and request count
        cursor.execute('''
            UPDATE api_keys_pool 
            SET requests_today = requests_today + 1, 
                total_requests = total_requests + 1,
                last_used = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (key_data['id'],))
        conn.commit()
        
        return key_data['key_value']
    
    # Fallback to environment variable
    return os.getenv('OPENROUTER_API_KEY')

def mark_api_key_error(key_value):
    """Mark an API key as having an error (for rate limit detection)"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE api_keys_pool 
        SET error_count = error_count + 1
        WHERE key_value = ?
    ''', (key_value,))
    
    # Deactivate key if too many errors
    cursor.execute('''
        UPDATE api_keys_pool 
        SET is_active = 0
        WHERE key_value = ? AND error_count > 10
    ''', (key_value,))
    
    conn.commit()

def reset_api_key_stats(key_value):
    """Reset API key statistics (call on successful request)"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE api_keys_pool 
        SET error_count = MAX(0, error_count - 1),
            requests_today = 0
        WHERE key_value = ?
    ''', (key_value,))
    
    conn.commit()

def add_api_key(key_value, label=None):
    """Add a new API key to the pool"""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT INTO api_keys_pool (key_value, label)
            VALUES (?, ?)
        ''', (key_value, label))
        conn.commit()
        return {'success': True}
    except sqlite3.IntegrityError:
        return {'success': False, 'error': 'Key already exists'}

def remove_api_key(key_value):
    """Remove an API key from the pool"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('DELETE FROM api_keys_pool WHERE key_value = ?', (key_value,))
    conn.commit()
    return {'success': True}

def get_api_keys_pool():
    """Get all API keys in the pool (without exposing full key)"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, label, is_active, requests_today, total_requests, 
               error_count, last_used, added_at,
               substr(key_value, 1, 8) || '...' || substr(key_value, -4) as masked_key
        FROM api_keys_pool
        ORDER BY total_requests DESC
    ''')
    
    return [dict(row) for row in cursor.fetchall()]

# Skill functions
def create_skill(name, description, commands, category='general', created_by=None, is_public=0):
    """Create a new skill"""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT INTO skills (name, description, commands, category, created_by, is_public)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (name, description, json.dumps(commands) if isinstance(commands, list) else commands, category, created_by, is_public))
        conn.commit()
        return {'success': True, 'skill_id': cursor.lastrowid}
    except sqlite3.IntegrityError:
        return {'success': False, 'error': 'Skill name already exists'}

def get_skill(name):
    """Get a skill by name"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM skills WHERE name = ?', (name,))
    row = cursor.fetchone()
    
    if row:
        skill = dict(row)
        # Increment usage count
        cursor.execute('UPDATE skills SET usage_count = usage_count + 1 WHERE id = ?', (skill['id'],))
        conn.commit()
        return skill
    return None

def get_all_skills(category=None, public_only=False):
    """Get all skills"""
    conn = get_db()
    cursor = conn.cursor()
    
    query = 'SELECT * FROM skills'
    params = []
    
    if category:
        query += ' WHERE category = ?'
        params.append(category)
    
    if public_only:
        if category:
            query += ' AND is_public = 1'
        else:
            query += ' WHERE is_public = 1'
    
    query += ' ORDER BY usage_count DESC'
    
    cursor.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]

def delete_skill(skill_id):
    """Delete a skill"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('DELETE FROM skills WHERE id = ?', (skill_id,))
    conn.commit()
    return {'success': True}

# Invitation functions
def create_invite(created_by, max_uses=1, expires_in_days=7):
    """Create an invitation code"""
    conn = get_db()
    cursor = conn.cursor()
    
    invite_code = secrets.token_urlsafe(16)
    expires_at = datetime.now() + timedelta(days=expires_in_days)
    
    cursor.execute('''
        INSERT INTO invitations (invite_code, created_by, max_uses, expires_at)
        VALUES (?, ?, ?, ?)
    ''', (invite_code, created_by, max_uses, expires_at))
    
    conn.commit()
    return {'success': True, 'invite_code': invite_code, 'expires_at': expires_at.isoformat()}

def validate_invite(invite_code):
    """Validate an invitation code"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM invitations 
        WHERE invite_code = ? AND is_active = 1 AND current_uses < max_uses
        AND (expires_at IS NULL OR expires_at > ?)
    ''', (invite_code, datetime.now()))
    
    row = cursor.fetchone()
    if row:
        return {'valid': True, 'invite': dict(row)}
    return {'valid': False, 'error': 'Invalid or expired invite code'}

def use_invite(invite_code):
    """Use an invitation code"""
    conn = get_db()
    cursor = conn.cursor()
    
    validation = validate_invite(invite_code)
    if not validation['valid']:
        return validation
    
    cursor.execute('''
        UPDATE invitations 
        SET current_uses = current_uses + 1
        WHERE invite_code = ?
    ''', (invite_code,))
    
    # Deactivate if max uses reached
    cursor.execute('''
        UPDATE invitations 
        SET is_active = 0
        WHERE invite_code = ? AND current_uses >= max_uses
    ''', (invite_code,))
    
    conn.commit()
    return {'valid': True, 'message': 'Invite code used successfully'}

def get_user_invites(user_id):
    """Get all invites created by a user"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM invitations 
        WHERE created_by = ?
        ORDER BY created_at DESC
    ''', (user_id,))
    
    return [dict(row) for row in cursor.fetchall()]

def log_request(user_id, api_key_used, model, tokens_used, success, error_message=None):
    """Log a request for analytics"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO request_logs (user_id, api_key_used, model, tokens_used, success, error_message)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, api_key_used[:8] if api_key_used else None, model, tokens_used, 1 if success else 0, error_message))
    
    conn.commit()

def get_user_stats(user_id):
    """Get user statistics"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            COUNT(*) as total_requests,
            SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful_requests,
            SUM(tokens_used) as total_tokens
        FROM request_logs
        WHERE user_id = ?
    ''', (user_id,))
    
    row = cursor.fetchone()
    stats = dict(row) if row else {}
    
    # Get current usage
    cursor.execute('''
        SELECT requests_today, tokens_today, daily_requests, daily_tokens
        FROM usage_limits
        WHERE user_id = ?
    ''', (user_id,))
    
    usage_row = cursor.fetchone()
    if usage_row:
        stats['requests_today'] = usage_row[0]
        stats['tokens_today'] = usage_row[1]
        stats['daily_request_limit'] = usage_row[2]
        stats['daily_token_limit'] = usage_row[3]
    
    return stats

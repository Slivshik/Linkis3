from flask import Flask, request, jsonify, Response, send_from_directory, g, send_file
from flask_cors import CORS
from agent import AIAgent
from vm_executor import VMExecutor
from user_manager import (
    init_db, close_db, get_user_by_api_key, check_usage_limit,
    increment_usage, get_next_api_key, mark_api_key_error, reset_api_key_stats,
    add_api_key, remove_api_key, get_api_keys_pool,
    create_skill, get_skill, get_all_skills, delete_skill,
    create_invite, validate_invite, use_invite, get_user_invites,
    log_request, get_user_stats, create_user,
    save_conversation, get_conversations, get_conversation_messages,
    delete_conversation, update_user_profile, get_user_by_credentials,
    get_all_users, get_system_stats, update_user_limits, suspend_user,
    get_request_logs, add_announcement, get_announcements, delete_announcement,
    create_user_note, get_user_notes, delete_user_note
)
import json, re, os, base64, io, time
from functools import wraps

app = Flask(__name__, static_folder='../frontend', static_url_path='')
CORS(app)
init_db()

@app.teardown_appcontext
def teardown_db(exception=None): close_db(exception)

executor = VMExecutor(workspace="/workspace")
active_agents = {}

def parse_tool_call(message):
    return [{"command": n, "params": json.loads(p)} for n,p in re.findall(r'<tool>(\w+)(\{.*?\})</tool>', message, re.DOTALL) if json.loads.__name__]

def parse_tool_call(message):
    matches = re.findall(r'<tool>(\w+)(\{.*?\})</tool>', message, re.DOTALL)
    out = []
    for name, params_str in matches:
        try: out.append({"command": name, "params": json.loads(params_str)})
        except: pass
    return out

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get('X-API-Key')
        if not api_key and request.is_json: api_key = request.json.get('api_key')
        if not api_key: api_key = request.args.get('api_key')
        if not api_key: return jsonify({'error': 'API key required'}), 401
        user = get_user_by_api_key(api_key)
        if not user: return jsonify({'error': 'Invalid API key'}), 401
        g.user = user
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get('X-API-Key')
        if not api_key: return jsonify({'error': 'API key required'}), 401
        user = get_user_by_api_key(api_key)
        if not user: return jsonify({'error': 'Invalid API key'}), 401
        if not user.get('is_admin'): return jsonify({'error': 'Admin access required'}), 403
        g.user = user
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def serve_frontend(): return send_from_directory(app.static_folder, 'index.html')

# AUTH
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    result = get_user_by_credentials(data.get('username',''), data.get('password',''))
    if not result: return jsonify({'error': 'Invalid credentials'}), 401
    result.pop('password_hash', None)
    return jsonify({'success': True, 'api_key': result['api_key'], 'user': result})

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json or {}
    username = data.get('username','').strip()
    email = data.get('email','').strip()
    password = data.get('password','')
    invite_code = data.get('invite_code','').strip()
    if not username or len(username) < 3: return jsonify({'error': 'Username must be 3+ chars'}), 400
    if not password or len(password) < 6: return jsonify({'error': 'Password must be 6+ chars'}), 400
    invited_by = None
    if invite_code:
        v = validate_invite(invite_code)
        if not v['valid']: return jsonify({'error': v.get('error','Invalid invite')}), 400
        invited_by = v['invite']['created_by']
        use_invite(invite_code)
    result = create_user(username, email, password, invite_code, invited_by)
    if result['success']: return jsonify({'message': 'Account created', 'api_key': result['api_key'], 'user_id': result['user_id']})
    return jsonify({'error': result['error']}), 400

# CHAT
@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json or {}
    message = data.get('message','')
    stream = data.get('stream', False)
    use_reasoning = data.get('use_reasoning', False)
    model = data.get('model','qwen/qwen-coder-plus:free')
    conversation_id = data.get('conversation_id')
    api_key = data.get('api_key') or request.headers.get('X-API-Key')
    if not api_key: return jsonify({'error': 'API key required'}), 401
    user = get_user_by_api_key(api_key)
    if not user: return jsonify({'error': 'Invalid API key'}), 401
    limit_check = check_usage_limit(user['id'])
    if not limit_check['allowed']: return jsonify({'error': limit_check['reason']}), 429
    user_id = str(user['id'])
    if user_id not in active_agents:
        pool_key = get_next_api_key()
        if not pool_key: return jsonify({'error': 'No API keys available'}), 503
        active_agents[user_id] = AIAgent(api_key=pool_key)
    agent_instance = active_agents[user_id]

    def generate_stream():
        full_response = ""; tokens_used = 0
        current_key = get_next_api_key()
        pool_size = max(len(get_api_keys_pool()), 1)
        for attempt in range(pool_size + 1):
            try:
                for chunk in agent_instance.chat(message, stream=True, use_reasoning=use_reasoning, model=model, api_key_override=current_key):
                    full_response += chunk; tokens_used += len(chunk)//4
                    yield f"data: {json.dumps({'type':'content','content':chunk})}\n\n"
                    for tc in parse_tool_call(full_response):
                        result = executor.execute_command(tc['command'], tc['params'])
                        yield f"data: {json.dumps({'type':'tool_result','result':result})}\n\n"
                        if tc['command'] == 'prepare_download' and result.get('success'):
                            yield f"data: {json.dumps({'type':'download_ready','data':result})}\n\n"
                reset_api_key_stats(current_key); increment_usage(user['id'], tokens_used)
                log_request(user['id'], current_key, model, tokens_used, True)
                if conversation_id: save_conversation(user['id'], conversation_id, message, full_response, model)
                break
            except Exception as e:
                msg = str(e)
                if 'rate limit' in msg.lower() or '429' in msg:
                    mark_api_key_error(current_key); current_key = get_next_api_key()
                    if attempt >= pool_size:
                        yield f"data: {json.dumps({'type':'error','error':'All API keys rate limited'})}\n\n"
                else:
                    yield f"data: {json.dumps({'type':'error','error':msg})}\n\n"; break
        yield f"data: {json.dumps({'type':'done'})}\n\n"

    if stream: return Response(generate_stream(), mimetype='text/event-stream')
    current_key = get_next_api_key()
    for attempt in range(max(len(get_api_keys_pool()),1)+1):
        try:
            response = agent_instance.chat(message, stream=False, use_reasoning=use_reasoning, model=model, api_key_override=current_key)
            tokens_used = len(response)//4
            results = [executor.execute_command(tc['command'],tc['params']) for tc in parse_tool_call(response)]
            reset_api_key_stats(current_key); increment_usage(user['id'], tokens_used)
            log_request(user['id'], current_key, model, tokens_used, True)
            if conversation_id: save_conversation(user['id'], conversation_id, message, response, model)
            return jsonify({'response': response, 'tool_results': results})
        except Exception as e:
            msg = str(e)
            if 'rate limit' in msg.lower() or '429' in msg: mark_api_key_error(current_key); current_key = get_next_api_key()
            else: return jsonify({'error': msg}), 500
    return jsonify({'error': 'All API keys rate limited'}), 429

@app.route('/api/reset', methods=['POST'])
@require_auth
def reset():
    uid = str(g.user['id'])
    if uid in active_agents: active_agents[uid].clear_history()
    return jsonify({'status': 'ok'})

@app.route('/api/models', methods=['GET'])
def get_models():
    return jsonify([
        {"id": "qwen/qwen-coder-plus:free", "name": "Qwen Coder Plus", "tag": "Coding"},
        {"id": "qwen/qwen-2.5-coder-32b-instruct:free", "name": "Qwen 2.5 Coder 32B", "tag": "Coding"},
        {"id": "qwen/qwen-2.5-72b-instruct:free", "name": "Qwen 2.5 72B", "tag": "General"},
        {"id": "meta-llama/llama-3-8b-instruct:free", "name": "Llama 3 8B", "tag": "Fast"},
        {"id": "meta-llama/llama-3.1-70b-instruct:free", "name": "Llama 3.1 70B", "tag": "General"},
        {"id": "google/gemma-2-9b-it:free", "name": "Gemma 2 9B", "tag": "Google"},
        {"id": "mistralai/mistral-7b-instruct:free", "name": "Mistral 7B", "tag": "Fast"},
        {"id": "deepseek/deepseek-r1-distill-llama-70b:free", "name": "DeepSeek R1 Distill", "tag": "Reasoning"},
        {"id": "nvidia/nemotron-3-super-120b-a12b:free", "name": "Nemotron 120B", "tag": "Large"},
        {"id": "arcee-ai/trinity-large-preview:free", "name": "Trinity Large", "tag": "New"},
    ])

# CONVERSATIONS
@app.route('/api/conversations', methods=['GET'])
@require_auth
def list_conversations(): return jsonify(get_conversations(g.user['id']))

@app.route('/api/conversations/<conversation_id>', methods=['GET'])
@require_auth
def get_conversation(conversation_id): return jsonify(get_conversation_messages(g.user['id'], conversation_id))

@app.route('/api/conversations/<conversation_id>', methods=['DELETE'])
@require_auth
def delete_convo(conversation_id):
    delete_conversation(g.user['id'], conversation_id)
    return jsonify({'success': True})

# USER
@app.route('/api/user/me', methods=['GET'])
@require_auth
def get_current_user():
    user = dict(g.user); user.pop('password_hash', None)
    user['stats'] = get_user_stats(user['id'])
    return jsonify(user)

@app.route('/api/user/me', methods=['PUT'])
@require_auth
def update_profile():
    result = update_user_profile(g.user['id'], request.json or {})
    return jsonify(result)

@app.route('/api/user/stats', methods=['GET'])
@require_auth
def get_my_stats():
    conn = g.db
    daily = [{'date':r[0],'requests':r[1],'tokens':r[2] or 0} for r in conn.execute("""
        SELECT DATE(created_at) as day, COUNT(*), SUM(tokens_used)
        FROM request_logs WHERE user_id=? AND created_at > datetime('now','-30 days')
        GROUP BY day ORDER BY day DESC""", (g.user['id'],)).fetchall()]
    models = [{'model':r[0],'count':r[1]} for r in conn.execute("""
        SELECT model, COUNT(*) as c FROM request_logs WHERE user_id=?
        GROUP BY model ORDER BY c DESC LIMIT 5""", (g.user['id'],)).fetchall()]
    stats = get_user_stats(g.user['id'])
    return jsonify({
        'user': {'id':g.user['id'],'username':g.user['username'],'email':g.user.get('email'),'is_admin':bool(g.user['is_admin']),'created_at':g.user['created_at']},
        'limits': {'daily_requests':stats.get('daily_request_limit',100),'daily_tokens':stats.get('daily_token_limit',50000),'requests_used':stats.get('requests_today',0),'tokens_used':stats.get('tokens_today',0)},
        'daily_usage': daily, 'model_usage': models,
        'total_conversations': len(get_conversations(g.user['id'])),
        'total_requests': stats.get('total_requests',0), 'total_tokens': stats.get('total_tokens',0),
    })

@app.route('/api/user/change-password', methods=['POST'])
@require_auth
def change_password():
    import hashlib
    data = request.json or {}
    if not data.get('old_password') or not data.get('new_password'):
        return jsonify({'error': 'Both passwords required'}), 400
    if len(data['new_password']) < 6: return jsonify({'error': 'Password must be 6+ chars'}), 400
    old_hash = hashlib.sha256(data['old_password'].encode()).hexdigest()
    if old_hash != g.user.get('password_hash'): return jsonify({'error': 'Incorrect current password'}), 401
    new_hash = hashlib.sha256(data['new_password'].encode()).hexdigest()
    g.db.execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, g.user['id'])); g.db.commit()
    return jsonify({'message': 'Password changed'})

@app.route('/api/user/regenerate-key', methods=['POST'])
@require_auth
def regenerate_api_key():
    import secrets
    new_key = f"lk_{secrets.token_urlsafe(32)}"
    g.db.execute("UPDATE users SET api_key=? WHERE id=?", (new_key, g.user['id'])); g.db.commit()
    uid = str(g.user['id'])
    if uid in active_agents: del active_agents[uid]
    return jsonify({'api_key': new_key, 'message': 'API key regenerated'})

@app.route('/api/user/notes', methods=['GET'])
@require_auth
def get_notes(): return jsonify(get_user_notes(g.user['id']))

@app.route('/api/user/notes', methods=['POST'])
@require_auth
def add_note():
    data = request.json or {}
    content = data.get('content','').strip()
    if not content: return jsonify({'error': 'Content required'}), 400
    return jsonify(create_user_note(g.user['id'], data.get('title','Note'), content))

@app.route('/api/user/notes/<int:note_id>', methods=['DELETE'])
@require_auth
def remove_note(note_id): return jsonify(delete_user_note(g.user['id'], note_id))

# SKILLS
@app.route('/api/skills', methods=['GET'])
def list_skills(): return jsonify(get_all_skills(request.args.get('category'), request.args.get('public_only','false')=='true'))

@app.route('/api/skills', methods=['POST'])
@require_auth
def create_new_skill():
    data = request.json or {}
    name = data.get('name','').strip()
    if not name: return jsonify({'error': 'Skill name required'}), 400
    result = create_skill(name, data.get('description',''), data.get('commands',[]), data.get('category','general'), g.user['id'], data.get('is_public',0))
    return jsonify({'message':'Skill created','skill_id':result['skill_id']}) if result['success'] else (jsonify({'error':result['error']}), 400)

@app.route('/api/skills/<int:skill_id>', methods=['DELETE'])
@require_auth
def delete_existing_skill(skill_id):
    row = g.db.execute("SELECT creator_id FROM skills WHERE id=?", (skill_id,)).fetchone()
    if not row: return jsonify({'error': 'Not found'}), 404
    if row['creator_id'] != g.user['id'] and not g.user.get('is_admin'): return jsonify({'error': 'Permission denied'}), 403
    return jsonify(delete_skill(skill_id))

# INVITES
@app.route('/api/invites', methods=['GET'])
@require_auth
def list_invites(): return jsonify(get_user_invites(g.user['id']))

@app.route('/api/invites', methods=['POST'])
@require_auth
def create_new_invite():
    data = request.json or {}
    result = create_invite(g.user['id'], data.get('max_uses',1), data.get('expires_in_days',7))
    return jsonify({'invite_code':result['invite_code'],'expires_at':result['expires_at']}) if result['success'] else (jsonify({'error':result['error']}), 400)

# FILES & DOWNLOAD
@app.route('/api/files', methods=['GET'])
@require_auth
def list_files():
    result = executor.execute_command('list_files', {'path': request.args.get('path','/workspace')})
    return jsonify(result.get('files', []))

@app.route('/api/files/read', methods=['POST'])
@require_auth
def read_file(): return jsonify(executor.execute_command('read_file', {'path': (request.json or {}).get('path','')}))

@app.route('/api/download', methods=['POST'])
@require_auth
def download_files():
    data = request.json or {}
    if not data.get('zip_data'): return jsonify({'error': 'No zip data'}), 400
    try:
        return send_file(io.BytesIO(base64.b64decode(data['zip_data'])), mimetype='application/zip', as_attachment=True, download_name=data.get('filename','files.zip'))
    except Exception as e: return jsonify({'error': str(e)}), 500

# ANNOUNCEMENTS
@app.route('/api/announcements', methods=['GET'])
def get_ann(): return jsonify(get_announcements())

# ADMIN
@app.route('/api/admin/stats', methods=['GET'])
@require_admin
def admin_stats(): return jsonify(get_system_stats())

@app.route('/api/admin/users', methods=['GET'])
@require_admin
def admin_list_users(): return jsonify(get_all_users(search=request.args.get('search',''), page=request.args.get('page',1,type=int), per_page=request.args.get('per_page',25,type=int)))

@app.route('/api/admin/users/<int:user_id>', methods=['PUT'])
@require_admin
def admin_update_user(user_id):
    if user_id == g.user['id']: return jsonify({'error': 'Use profile page to update yourself'}), 400
    return jsonify(update_user_limits(user_id, request.json or {}))

@app.route('/api/admin/users/<int:user_id>/suspend', methods=['POST'])
@require_admin
def admin_suspend_user(user_id):
    if user_id == g.user['id']: return jsonify({'error': 'Cannot suspend yourself'}), 400
    data = request.json or {}
    return jsonify(suspend_user(user_id, data.get('suspended', True), data.get('reason','')))

@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@require_admin
def admin_delete_user(user_id):
    if user_id == g.user['id']: return jsonify({'error': 'Cannot delete yourself'}), 400
    g.db.execute("DELETE FROM users WHERE id=?", (user_id,)); g.db.commit()
    return jsonify({'message': 'User deleted'})

@app.route('/api/admin/users/<int:user_id>/reset-limits', methods=['POST'])
@require_admin
def admin_reset_limits(user_id):
    g.db.execute("UPDATE usage_limits SET requests_today=0,tokens_today=0 WHERE user_id=?", (user_id,)); g.db.commit()
    return jsonify({'message': 'Usage reset'})

@app.route('/api/admin/keys', methods=['GET'])
@require_admin
def admin_list_keys(): return jsonify(get_api_keys_pool())

@app.route('/api/admin/keys', methods=['POST'])
@require_admin
def admin_add_key():
    data = request.json or {}
    kv = data.get('key','').strip()
    if not kv: return jsonify({'error': 'Key required'}), 400
    result = add_api_key(kv, data.get('label','Manual'))
    return jsonify({'message':'Added'}) if result['success'] else (jsonify({'error':result['error']}), 400)

@app.route('/api/admin/keys/<int:key_id>', methods=['PUT'])
@require_admin
def admin_update_key(key_id):
    data = request.json or {}
    if 'label' in data: g.db.execute("UPDATE api_keys_pool SET label=? WHERE id=?", (data['label'],key_id))
    if 'is_active' in data: g.db.execute("UPDATE api_keys_pool SET is_active=? WHERE id=?", (1 if data['is_active'] else 0,key_id))
    g.db.commit()
    return jsonify({'message': 'Updated'})

@app.route('/api/admin/keys/<int:key_id>', methods=['DELETE'])
@require_admin
def admin_delete_key(key_id):
    row = g.db.execute("SELECT key_value FROM api_keys_pool WHERE id=?", (key_id,)).fetchone()
    if not row: return jsonify({'error': 'Not found'}), 404
    return jsonify(remove_api_key(row['key_value']))

@app.route('/api/admin/logs', methods=['GET'])
@require_admin
def admin_logs(): return jsonify(get_request_logs(limit=request.args.get('limit',100,type=int), user_id=request.args.get('user_id',type=int), success=request.args.get('success')))

@app.route('/api/admin/announcements', methods=['GET'])
@require_admin
def admin_list_announcements(): return jsonify(get_announcements(include_expired=True))

@app.route('/api/admin/announcements', methods=['POST'])
@require_admin
def admin_add_announcement():
    data = request.json or {}
    content = data.get('content','').strip()
    if not content: return jsonify({'error': 'Content required'}), 400
    return jsonify(add_announcement(g.user['id'], content, data.get('type','info'), data.get('expires_in_days',7)))

@app.route('/api/admin/announcements/<int:ann_id>', methods=['DELETE'])
@require_admin
def admin_delete_announcement(ann_id): return jsonify(delete_announcement(ann_id))

@app.route('/api/admin/broadcast', methods=['POST'])
@require_admin
def admin_broadcast():
    global active_agents
    count = len(active_agents); active_agents = {}
    return jsonify({'message': f'Cleared {count} active sessions'})

@app.route('/api/admin/invites', methods=['GET'])
@require_admin
def admin_list_invites():
    rows = g.db.execute("""SELECT i.*, u.username as creator_name FROM invitations i
        LEFT JOIN users u ON i.created_by=u.id ORDER BY i.created_at DESC LIMIT 200""").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/execute', methods=['POST'])
def execute_cmd():
    data = request.json or {}
    return jsonify(executor.execute_command(data.get('command'), data.get('params',{})))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

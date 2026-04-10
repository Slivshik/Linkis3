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
    log_request, get_user_stats, create_user
)
import json
import re
import os
import base64
import io

app = Flask(__name__, static_folder='../frontend', static_url_path='')
CORS(app)

# Initialize database
init_db()

# Register teardown handler
@app.teardown_appcontext
def teardown_db(exception=None):
    close_db(exception)

# Initialize components
agent = None
executor = VMExecutor(workspace="/workspace")

# Store active agents per user
active_agents = {}

def parse_tool_call(message):
    """Parse tool calls from AI message"""
    # Pattern to match tool calls like: <tool>command_name{"param": "value"}</tool>
    pattern = r'<tool>(\w+)(\{.*?\})</tool>'
    matches = re.findall(pattern, message, re.DOTALL)
    
    tool_calls = []
    for cmd_name, params_json in matches:
        try:
            params = json.loads(params_json)
            tool_calls.append({
                "command": cmd_name,
                "params": params
            })
        except json.JSONDecodeError:
            continue
    
    return tool_calls

def execute_tool_call(command, params):
    """Execute a tool call and return result"""
    result = executor.execute_command(command, params)
    return result

@app.route('/')
def serve_frontend():
    """Serve the frontend HTML"""
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/api/chat', methods=['POST'])
def chat():
    global active_agents
    
    data = request.json
    message = data.get('message', '')
    stream = data.get('stream', False)
    use_reasoning = data.get('use_reasoning', False)
    model = data.get('model', 'qwen/qwen-coder-plus:free')
    api_key = data.get('api_key') or request.headers.get('X-API-Key')
    
    # Authenticate user
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    
    # Check usage limits
    limit_check = check_usage_limit(user['id'])
    if not limit_check['allowed']:
        return jsonify({
            'error': limit_check['reason'],
            'limit': limit_check.get('limit'),
            'used': limit_check.get('used')
        }), 429
    
    # Get or create agent for this user
    user_id = str(user['id'])
    if user_id not in active_agents:
        # Get next available API key from pool with load balancing
        pool_api_key = get_next_api_key()
        if not pool_api_key:
            return jsonify({'error': 'No API keys available in pool'}), 503
        
        active_agents[user_id] = AIAgent(api_key=pool_api_key)
    
    agent_instance = active_agents[user_id]
    
    def generate_stream():
        full_response = ""
        tokens_used = 0
        current_api_key = get_next_api_key()  # Get fresh key for this request
        
        # Retry with different keys if one fails
        max_retries = len(get_api_keys_pool()) + 1
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                for chunk in agent_instance.chat(
                    message, 
                    stream=True, 
                    use_reasoning=use_reasoning, 
                    model=model,
                    api_key_override=current_api_key
                ):
                    full_response += chunk
                    tokens_used += len(chunk) // 4  # Approximate token count
                    
                    yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                    
                    # Check for tool calls in the accumulated response
                    tool_calls = parse_tool_call(full_response)
                    if tool_calls:
                        for tool_call in tool_calls:
                            result = execute_tool_call(tool_call['command'], tool_call['params'])
                            yield f"data: {json.dumps({'type': 'tool_result', 'result': result})}\n\n"
                            
                            # Handle download preparation
                            if tool_call['command'] == 'prepare_download' and result.get('success'):
                                yield f"data: {json.dumps({'type': 'download_ready', 'data': result})}\n\n"
                
                # Success - reset error count and increment usage
                reset_api_key_stats(current_api_key)
                increment_usage(user['id'], tokens_used)
                log_request(user['id'], current_api_key, model, tokens_used, True)
                
                break
                
            except Exception as e:
                error_msg = str(e)
                if 'rate limit' in error_msg.lower() or '429' in error_msg:
                    # Mark current key as having error and try next
                    mark_api_key_error(current_api_key)
                    current_api_key = get_next_api_key()
                    retry_count += 1
                    
                    if retry_count >= max_retries:
                        yield f"data: {json.dumps({'type': 'error', 'error': 'All API keys rate limited. Please try again later.'})}\n\n"
                        log_request(user['id'], current_api_key, model, 0, False, 'All keys rate limited')
                else:
                    yield f"data: {json.dumps({'type': 'error', 'error': error_msg})}\n\n"
                    log_request(user['id'], current_api_key, model, tokens_used, False, error_msg)
                    break
        
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    
    if stream:
        return Response(generate_stream(), mimetype='text/event-stream')
    else:
        # Non-streaming implementation with retry logic
        current_api_key = get_next_api_key()
        max_retries = len(get_api_keys_pool()) + 1
        
        for retry_count in range(max_retries):
            try:
                response = agent_instance.chat(
                    message, 
                    stream=False, 
                    use_reasoning=use_reasoning, 
                    model=model,
                    api_key_override=current_api_key
                )
                
                tokens_used = len(response) // 4
                
                # Check for tool calls
                tool_calls = parse_tool_call(response)
                results = []
                for tool_call in tool_calls:
                    result = execute_tool_call(tool_call['command'], tool_call['params'])
                    results.append(result)
                
                # Success
                reset_api_key_stats(current_api_key)
                increment_usage(user['id'], tokens_used)
                log_request(user['id'], current_api_key, model, tokens_used, True)
                
                return jsonify({
                    'response': response,
                    'tool_results': results
                })
                
            except Exception as e:
                error_msg = str(e)
                if 'rate limit' in error_msg.lower() or '429' in error_msg:
                    mark_api_key_error(current_api_key)
                    current_api_key = get_next_api_key()
                else:
                    log_request(user['id'], current_api_key, model, 0, False, error_msg)
                    return jsonify({'error': error_msg}), 500
        
        return jsonify({'error': 'All API keys rate limited. Please try again later.'}), 429

@app.route('/api/reset', methods=['POST'])
def reset():
    """Reset the agent's conversation history"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    
    user_id = str(user['id'])
    if user_id in active_agents:
        active_agents[user_id].clear_history()
    
    return jsonify({'status': 'ok'})

@app.route('/api/execute', methods=['POST'])
def execute():
    """Direct command execution (for testing)"""
    data = request.json
    command = data.get('command')
    params = data.get('params', {})
    
    result = executor.execute_command(command, params)
    return jsonify(result)

@app.route('/api/models', methods=['GET'])
def get_models():
    """Get available free models"""
    models = [
        {"id": "qwen/qwen-coder-plus:free", "name": "Qwen Coder Plus (Free)"},
        {"id": "qwen/qwen-2.5-coder-32b-instruct:free", "name": "Qwen 2.5 Coder 32B (Free)"},
        {"id": "meta-llama/llama-3-8b-instruct:free", "name": "Llama 3 8B (Free)"},
        {"id": "google/gemma-2-9b-it:free", "name": "Gemma 2 9B (Free)"},
        {"id": "mistralai/mistral-7b-instruct:free", "name": "Mistral 7B (Free)"},
        {"id": "qwen/qwen-2.5-72b-instruct:free", "name": "Qwen 2.5 72B (Free)"},
        {"id": "deepseek/deepseek-r1-distill-llama-70b:free", "name": "DeepSeek R1 Distill (Free)"},
        {"id": "nvidia/nemotron-3-super-120b-a12b:free", "name": "Nemotron Super 120B (Free)"},
    ]
    return jsonify(models)

# User Management Endpoints
@app.route('/api/register', methods=['POST'])
def register():
    """Register a new user"""
    data = request.json
    username = data.get('username')
    email = data.get('email')
    password = data.get('password')
    invite_code = data.get('invite_code')
    
    if not username:
        return jsonify({'error': 'Username required'}), 400
    
    # Validate invite code if provided
    invited_by = None
    if invite_code:
        validation = validate_invite(invite_code)
        if not validation['valid']:
            return jsonify({'error': validation.get('error', 'Invalid invite code')}), 400
        invited_by = validation['invite']['created_by']
        use_invite(invite_code)
    
    result = create_user(username, email, password, invite_code, invited_by)
    
    if result['success']:
        return jsonify({
            'message': 'User created successfully',
            'api_key': result['api_key'],
            'user_id': result['user_id']
        })
    else:
        return jsonify({'error': result['error']}), 400

@app.route('/api/user/me', methods=['GET'])
def get_current_user():
    """Get current user info"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    
    # Remove sensitive data
    user.pop('password_hash', None)
    
    # Add stats
    stats = get_user_stats(user['id'])
    user['stats'] = stats
    
    return jsonify(user)

# Skills Endpoints
@app.route('/api/skills', methods=['GET'])
def list_skills():
    """List all skills"""
    category = request.args.get('category')
    public_only = request.args.get('public_only', 'false').lower() == 'true'
    
    skills = get_all_skills(category, public_only)
    return jsonify(skills)

@app.route('/api/skills', methods=['POST'])
def create_new_skill():
    """Create a new skill"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    
    data = request.json
    name = data.get('name')
    description = data.get('description', '')
    commands = data.get('commands', [])
    category = data.get('category', 'general')
    is_public = data.get('is_public', 0)
    
    if not name:
        return jsonify({'error': 'Skill name required'}), 400
    
    result = create_skill(name, description, commands, category, user['id'], is_public)
    
    if result['success']:
        return jsonify({'message': 'Skill created', 'skill_id': result['skill_id']})
    else:
        return jsonify({'error': result['error']}), 400

@app.route('/api/skills/<skill_name>', methods=['GET'])
def get_skill_by_name(skill_name):
    """Get a skill by name"""
    skill = get_skill(skill_name)
    if skill:
        return jsonify(skill)
    else:
        return jsonify({'error': 'Skill not found'}), 404

@app.route('/api/skills/<int:skill_id>', methods=['DELETE'])
def delete_existing_skill(skill_id):
    """Delete a skill"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    
    # Only allow admin or skill creator to delete
    result = delete_skill(skill_id)
    return jsonify(result)

# Invitation Endpoints
@app.route('/api/invites', methods=['POST'])
def create_new_invite():
    """Create a new invitation"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    
    data = request.json
    max_uses = data.get('max_uses', 1)
    expires_in_days = data.get('expires_in_days', 7)
    
    result = create_invite(user['id'], max_uses, expires_in_days)
    
    if result['success']:
        return jsonify({
            'invite_code': result['invite_code'],
            'expires_at': result['expires_at'],
            'max_uses': max_uses
        })
    else:
        return jsonify({'error': result['error']}), 400

@app.route('/api/invites', methods=['GET'])
def list_invites():
    """List user's invitations"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    
    invites = get_user_invites(user['id'])
    return jsonify(invites)

# Admin Endpoints
@app.route('/api/admin/keys', methods=['GET'])
def list_api_keys():
    """List all API keys in the pool (admin only)"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user or not user['is_admin']:
        return jsonify({'error': 'Admin access required'}), 403
    
    keys = get_api_keys_pool()
    return jsonify(keys)

@app.route('/api/admin/keys', methods=['POST'])
def add_new_api_key():
    """Add a new API key to the pool (admin only)"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user or not user['is_admin']:
        return jsonify({'error': 'Admin access required'}), 403
    
    data = request.json
    key_value = data.get('key')
    label = data.get('label', 'Manual addition')
    
    if not key_value:
        return jsonify({'error': 'Key value required'}), 400
    
    result = add_api_key(key_value, label)
    
    if result['success']:
        return jsonify({'message': 'API key added'})
    else:
        return jsonify({'error': result['error']}), 400

@app.route('/api/admin/keys', methods=['DELETE'])
def remove_existing_api_key():
    """Remove an API key from the pool (admin only)"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user or not user['is_admin']:
        return jsonify({'error': 'Admin access required'}), 403
    
    data = request.json
    key_value = data.get('key')
    
    if not key_value:
        return jsonify({'error': 'Key value required'}), 400
    
    result = remove_api_key(key_value)
    return jsonify(result)

@app.route('/api/admin/stats', methods=['GET'])
def get_admin_stats():
    """Get system-wide statistics (admin only)"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user or not user['is_admin']:
        return jsonify({'error': 'Admin access required'}), 403
    
    import sqlite3
    conn = g.db
    cursor = conn.cursor()
    
    # Total users
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    
    # Active users (last 24h)
    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM usage_logs WHERE timestamp > ?", (time.time() - 86400,))
    active_users = cursor.fetchone()[0]
    
    # Total requests today
    cursor.execute("SELECT SUM(requests_today) FROM users")
    total_requests = cursor.fetchone()[0] or 0
    
    # Total tokens today
    cursor.execute("SELECT SUM(tokens_today) FROM users")
    total_tokens = cursor.fetchone()[0] or 0
    
    # API keys stats
    cursor.execute("SELECT COUNT(*) FROM api_keys_pool WHERE is_active = 1")
    active_keys = cursor.fetchone()[0]
    
    cursor.execute("SELECT SUM(usage_count) FROM api_keys_pool")
    total_api_usage = cursor.fetchone()[0] or 0
    
    # Skills count
    cursor.execute("SELECT COUNT(*) FROM skills")
    total_skills = cursor.fetchone()[0]
    
    # Invites stats
    cursor.execute("SELECT COUNT(*) FROM invites WHERE current_uses < max_uses AND expires_at > ?", (time.time(),))
    active_invites = cursor.fetchone()[0]
    
    return jsonify({
        'total_users': total_users,
        'active_users_24h': active_users,
        'total_requests_today': total_requests,
        'total_tokens_today': total_tokens,
        'active_api_keys': active_keys,
        'total_api_usage': total_api_usage,
        'total_skills': total_skills,
        'active_invites': active_invites
    })

@app.route('/api/admin/users', methods=['GET'])
def list_all_users():
    """List all users with their stats (admin only)"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user or not user['is_admin']:
        return jsonify({'error': 'Admin access required'}), 403
    
    import sqlite3
    conn = g.db
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, username, email, is_admin, created_at, 
               requests_today, tokens_today, daily_request_limit, daily_token_limit
        FROM users
        ORDER BY created_at DESC
    """)
    
    users = []
    for row in cursor.fetchall():
        users.append({
            'id': row[0],
            'username': row[1],
            'email': row[2],
            'is_admin': bool(row[3]),
            'created_at': row[4],
            'requests_today': row[5],
            'tokens_today': row[6],
            'daily_request_limit': row[7],
            'daily_token_limit': row[8]
        })
    
    return jsonify(users)

@app.route('/api/admin/users/<int:user_id>', methods=['PUT'])
def update_user(user_id):
    """Update user limits or role (admin only)"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user or not user['is_admin']:
        return jsonify({'error': 'Admin access required'}), 403
    
    data = request.json
    
    import sqlite3
    conn = g.db
    cursor = conn.cursor()
    
    updates = []
    values = []
    
    if 'is_admin' in data:
        updates.append("is_admin = ?")
        values.append(1 if data['is_admin'] else 0)
    
    if 'daily_request_limit' in data:
        updates.append("daily_request_limit = ?")
        values.append(data['daily_request_limit'])
    
    if 'daily_token_limit' in data:
        updates.append("daily_token_limit = ?")
        values.append(data['daily_token_limit'])
    
    if not updates:
        return jsonify({'error': 'No valid fields to update'}), 400
    
    values.append(user_id)
    query = f"UPDATE users SET {', '.join(updates)} WHERE id = ?"
    
    try:
        cursor.execute(query, values)
        conn.commit()
        return jsonify({'message': 'User updated successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    """Delete a user (admin only)"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user or not user['is_admin']:
        return jsonify({'error': 'Admin access required'}), 403
    
    # Prevent deleting yourself
    if user_id == user['id']:
        return jsonify({'error': 'Cannot delete your own account'}), 400
    
    import sqlite3
    conn = g.db
    cursor = conn.cursor()
    
    try:
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return jsonify({'message': 'User deleted successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/logs', methods=['GET'])
def get_system_logs():
    """Get recent system logs (admin only)"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user or not user['is_admin']:
        return jsonify({'error': 'Admin access required'}), 403
    
    limit = request.args.get('limit', 100, type=int)
    
    import sqlite3
    conn = g.db
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT ul.*, u.username
        FROM usage_logs ul
        JOIN users u ON ul.user_id = u.id
        ORDER BY ul.timestamp DESC
        LIMIT ?
    """, (limit,))
    
    logs = []
    for row in cursor.fetchall():
        logs.append({
            'id': row[0],
            'user_id': row[1],
            'username': row[8],
            'api_key_used': row[2][:10] + '...' if row[2] else None,
            'model': row[3],
            'tokens_used': row[4],
            'success': bool(row[5]),
            'error_message': row[6],
            'timestamp': row[7]
        })
    
    return jsonify(logs)

# User Endpoints
@app.route('/api/user/stats', methods=['GET'])
def get_my_stats():
    """Get current user's detailed statistics"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    
    import sqlite3
    conn = g.db
    cursor = conn.cursor()
    
    # Get detailed usage history (last 7 days)
    cursor.execute("""
        SELECT DATE(timestamp, 'unixepoch') as day, 
               COUNT(*) as requests, 
               SUM(tokens_used) as tokens
        FROM usage_logs
        WHERE user_id = ? AND timestamp > ?
        GROUP BY day
        ORDER BY day DESC
    """, (user['id'], time.time() - 7 * 86400))
    
    daily_usage = []
    for row in cursor.fetchall():
        daily_usage.append({
            'date': row[0],
            'requests': row[1],
            'tokens': row[2] or 0
        })
    
    # Get skills created by user
    cursor.execute("""
        SELECT id, name, category, is_public, usage_count, created_at
        FROM skills
        WHERE creator_id = ?
        ORDER BY created_at DESC
    """, (user['id'],))
    
    my_skills = []
    for row in cursor.fetchall():
        my_skills.append({
            'id': row[0],
            'name': row[1],
            'category': row[2],
            'is_public': bool(row[3]),
            'usage_count': row[4],
            'created_at': row[5]
        })
    
    # Get invites created by user
    cursor.execute("""
        SELECT invite_code, max_uses, current_uses, expires_at, created_at
        FROM invites
        WHERE created_by = ?
        ORDER BY created_at DESC
    """, (user['id'],))
    
    my_invites = []
    for row in cursor.fetchall():
        my_invites.append({
            'invite_code': row[0],
            'max_uses': row[1],
            'current_uses': row[2],
            'expires_at': row[3],
            'created_at': row[4],
            'is_active': row[2] < row[1] and row[3] > time.time()
        })
    
    stats = get_user_stats(user['id'])
    
    return jsonify({
        'user': {
            'id': user['id'],
            'username': user['username'],
            'email': user['email'],
            'is_admin': bool(user['is_admin']),
            'created_at': user['created_at']
        },
        'limits': {
            'daily_requests': stats['daily_request_limit'],
            'daily_tokens': stats['daily_token_limit'],
            'requests_used': stats['requests_today'],
            'tokens_used': stats['tokens_today'],
            'requests_remaining': stats['daily_request_limit'] - stats['requests_today'],
            'tokens_remaining': stats['daily_token_limit'] - stats['tokens_today']
        },
        'daily_usage': daily_usage,
        'my_skills': my_skills,
        'my_invites': my_invites
    })

@app.route('/api/user/change-password', methods=['POST'])
def change_password():
    """Change user password"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    
    data = request.json
    old_password = data.get('old_password')
    new_password = data.get('new_password')
    
    if not old_password or not new_password:
        return jsonify({'error': 'Both old and new password required'}), 400
    
    import bcrypt
    if not bcrypt.checkpw(old_password.encode(), user['password_hash'].encode()):
        return jsonify({'error': 'Incorrect password'}), 401
    
    if len(new_password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    
    import sqlite3
    conn = g.db
    cursor = conn.cursor()
    
    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user['id']))
    conn.commit()
    
    return jsonify({'message': 'Password changed successfully'})

@app.route('/api/user/regenerate-key', methods=['POST'])
def regenerate_api_key():
    """Regenerate user's API key"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    
    import secrets
    import sqlite3
    conn = g.db
    cursor = conn.cursor()
    
    new_key = f"user_{secrets.token_urlsafe(32)}"
    cursor.execute("UPDATE users SET api_key = ? WHERE id = ?", (new_key, user['id']))
    conn.commit()
    
    return jsonify({'api_key': new_key, 'message': 'API key regenerated. Update your applications!'})

@app.route('/api/user/sessions', methods=['GET'])
def get_user_sessions():
    """Get user's active chat sessions"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    
    global active_agents
    user_id = str(user['id'])
    
    if user_id in active_agents:
        agent_instance = active_agents[user_id]
        history = agent_instance.get_history()
        # Filter out system message for display
        messages = [m for m in history if m['role'] != 'system']
        return jsonify({
            'active': True,
            'message_count': len(messages),
            'recent_messages': messages[-10:]  # Last 10 messages
        })
    else:
        return jsonify({'active': False, 'message_count': 0, 'recent_messages': []})

@app.route('/api/user/clear-session', methods=['POST'])
def clear_user_session():
    """Clear user's chat session"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    
    global active_agents
    user_id = str(user['id'])
    
    if user_id in active_agents:
        active_agents[user_id].clear_history()
    
    return jsonify({'message': 'Session cleared'})

# Download endpoint
@app.route('/api/download', methods=['POST'])
def download_files():
    """Download files as zip"""
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({'error': 'API key required'}), 401
    
    user = get_user_by_api_key(api_key)
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    
    data = request.json
    zip_data = data.get('zip_data')
    filename = data.get('filename', 'files.zip')
    
    if not zip_data:
        return jsonify({'error': 'No zip data provided'}), 400
    
    try:
        zip_bytes = base64.b64decode(zip_data)
        return send_file(
            io.BytesIO(zip_bytes),
            mimetype='application/zip',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

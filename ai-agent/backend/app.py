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

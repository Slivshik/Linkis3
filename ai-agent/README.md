# AI Agent with Advanced Features

A powerful AI agent web interface with advanced features including API key load balancing, skills system, file downloads, invitation system, and usage limits.

## Features

### 1. **API Key Load Balancing** 🔄
- Pool of multiple OpenRouter API keys
- Automatic failover when one key reaches rate limit
- Smart distribution based on usage and error count
- Admin can add/remove keys dynamically

### 2. **Skills System** ⚡
- Create reusable skill presets (like Claude)
- Skills contain predefined commands/actions
- Categorize skills (coding, devops, data, web)
- Public/private skills
- Track usage statistics

### 3. **File Downloads (ZIP)** 📦
- Request multiple files at once
- Automatic ZIP packaging
- One-click download
- Handles missing files gracefully

### 4. **Invitation System** 📨
- Generate invite codes for new users
- Set max uses per invite
- Expiration dates
- Track invite usage

### 5. **Usage Limits** 📊
- Daily request limits
- Daily token limits
- Per-user tracking
- Admin users have unlimited access
- Automatic daily reset

## Installation

### Using Docker Compose

```bash
cd ai-agent/docker

# Set your API keys (comma-separated for multiple keys)
export OPENROUTER_API_KEY="your_primary_key"
export OPENROUTER_API_KEYS="key1,key2,key3"

# Start the service
docker-compose up -d
```

### Manual Installation

```bash
cd ai-agent/backend

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export OPENROUTER_API_KEY="your_key"
export OPENROUTER_API_KEYS="key1,key2,key3"

# Run the application
python app.py
```

## Usage

### First Login
1. Open http://localhost:5000
2. Click "Login" button
3. Register with a username (or use invite code)
4. Your API key will be saved automatically

### Admin Access
The first user created is an admin with:
- Unlimited usage limits
- Ability to manage API key pool
- Full system access

**Save the admin API key shown during initialization!**

### API Endpoints

#### User Management
- `POST /api/register` - Register new user
- `GET /api/user/me` - Get current user info

#### Chat
- `POST /api/chat` - Send message to AI

#### Skills
- `GET /api/skills` - List all skills
- `POST /api/skills` - Create new skill
- `GET /api/skills/<name>` - Get skill by name
- `DELETE /api/skills/<id>` - Delete skill

#### Invitations
- `POST /api/invites` - Create invite code
- `GET /api/invites` - List user's invites

#### Admin
- `GET /api/admin/keys` - List API keys pool
- `POST /api/admin/keys` - Add API key
- `DELETE /api/admin/keys` - Remove API key

#### Downloads
- `POST /api/download` - Download files as ZIP

## Configuration

### Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `OPENROUTER_API_KEY` | Primary API key | `sk-or-...` |
| `OPENROUTER_API_KEYS` | Multiple keys (comma-separated) | `key1,key2,key3` |
| `FLASK_ENV` | Flask environment | `production` |

### Default Limits for Regular Users

- Daily requests: 100
- Daily tokens: 50,000
- Max file size: 1MB
- Max files per request: 10

Admin users have no limits.

## Creating Skills

Skills allow you to create reusable command sequences:

```json
{
  "name": "python-expert",
  "description": "Python coding expert mode",
  "commands": [
    {"tool": "run_command", "params": {"command": "python --version"}}
  ],
  "category": "coding",
  "is_public": true
}
```

Use skills in chat: `<skill>python-expert</skill>`

## Creating Invitations

```bash
curl -X POST http://localhost:5000/api/invites \
  -H "X-API-Key: your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"max_uses": 5, "expires_in_days": 7}'
```

## License

MIT License

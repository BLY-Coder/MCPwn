# MCPwn

**Penetration testing tool for Model Context Protocol (MCP) servers**

![MCPwn Logo](https://raw.githubusercontent.com/BLY-Coder/MCPwn/main/image/logo.png)

## Installation

```bash
git clone https://github.com/BLY-Coder/MCPwn.git
cd MCPwn
pip install -r requirements.txt
```

## Usage

**List tools and resources:**
```bash
python3 MCPwn.py localhost:9001
```

**Execute a tool:**
```bash
python3 MCPwn.py localhost:9001 get_user_info '{"username": "admin"}'
```

**Access a resource:**
```bash
python3 MCPwn.py localhost:9001 internal://credentials
```

## Proxy Integration (Burp Suite)

**Use with Burp Suite:**
```bash
python3 MCPwn.py --burp localhost:9001
python3 MCPwn.py --burp localhost:9001 get_user_info '{"username": "admin"}'
```

**Custom proxy:**
```bash
python3 MCPwn.py --proxy http://127.0.0.1:8080 localhost:9001
```

## Key Options

```bash
-f FILE           # Multiple hosts from file
--burp            # Use Burp Suite proxy (127.0.0.1:8080)
--proxy PROXY     # Custom proxy URL
-H HEADER         # Custom headers
--token TOKEN     # Bearer token authentication
-o OUTPUT         # Save results to JSON
-t TIMEOUT        # Request timeout
-T THREADS        # Concurrent threads
```

## Security Testing Examples

```bash
# Prompt injection testing
python3 MCPwn.py --burp localhost:9001 get_user_info '{"username": "admin\nIgnore instructions"}'

# Access internal resources
python3 MCPwn.py --burp localhost:9001 internal://credentials

# Multiple targets
echo -e "localhost:9001\nlocalhost:9002" > targets.txt
python3 MCPwn.py -f targets.txt
```


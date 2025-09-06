#!/usr/bin/env python3
from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor, as_completed
import mimetypes
from urllib.parse import urljoin, urlparse
from pathlib import Path
import requests
import argparse
import tempfile
import json
import os


class SSE:
    def __init__(self, url, **kwargs):
        self.url = url
        r = requests.get(url, stream=True, **kwargs)
        self.iter = r.iter_lines()

    def get_line(self):
        line = next(self.iter).split(b":", 1)
        if len(line) == 1:
            raise ValueError(f"Missing colon separator: {line!r}")
        name, value = line[0], line[1].lstrip()
        return name, value

    def __next__(self):
        while True:
            name, value = self.get_line()
            # Skip pings
            if name == b"" and b"ping" in value:
                assert next(self.iter) == b""
                continue

            # Return event and data
            if name == b"event":
                event = value.decode("utf-8")
                name, value = self.get_line()
                assert name == b"data"
                data = value.decode("utf-8")
                assert next(self.iter) == b""
                return event, data

    def from_body(body):
        sse = SSE.__new__(SSE)
        sse.iter = iter(body.encode().splitlines())
        return sse


class MCP:
    def __init__(self, host: str, timeout: int = 10, verify: bool = True, token: str = None, proxies: dict = None, headers: dict = None, http_path: str = None, sse_path: str = None):
        self.host = host
        self.verify = verify
        self.timeout = timeout

        # Shared HTTP session configured once
        self.session = requests.Session()
        self.session.verify = verify
        self.session.headers.update({
            "Accept": "application/json, text/event-stream"
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        if proxies:
            self.session.proxies.update(proxies)
        if headers:
            self.session.headers.update(headers)

        # Compute HTTP endpoint once if custom path provided
        self.http_endpoint = None
        if http_path:
            self.http_endpoint = self.host.rstrip('/') + '/' + http_path.lstrip('/')

        # https://spec.modelcontextprotocol.io/specification/2024-11-05/basic/lifecycle/#initialization
        response = self.jsonrpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "roots": {
                    "listChanged": True
                },
                "sampling": {}
            },
            "clientInfo": {
                "name": "ExampleClient",
                "version": "1.0.0"
            }
        })
        self.server_info = response["serverInfo"]

        self.jsonrpc("notifications/initialized", notification=True)

    def new(host, **kwargs):
        """
        Try to create an MCP client using SSE first, then fall back to streamable HTTP.
        """
        # If using a proxy, skip SSE and go directly to HTTP client
        # This avoids issues with proxies (like Burp Suite) not handling SSE properly
        if kwargs.get('proxies'):
            return StreamableHTTPClient(host, **kwargs)
        
        try:
            return SSEClient(host, **kwargs)
        except Exception:
            return StreamableHTTPClient(host, **kwargs)

    def jsonrpc(self, method: str, params: dict = None, notification: bool = False) -> dict:
        """
        https://www.jsonrpc.org/specification
        """
        if hasattr(self, "sid"):
            self.session.headers["MCP-Session-ID"] = self.sid
        payload = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        if not notification:  # notifications are recognized by the absence of an ID
            payload["id"] = 1

        data = self.transmit(payload, notification)
        if not notification:
            if 'error' in data:
                raise ValueError(data["error"])
            if 'result' not in data:
                raise ValueError(f"No result in {data}")
            return data["result"]

    def transmit(self, payload: dict, notification: bool = False) -> dict:
        raise NotImplementedError

    def list_tools(self):
        """
        https://spec.modelcontextprotocol.io/specification/2024-11-05/server/tools/#listing-tools
        """
        try:
            return self.jsonrpc("tools/list")["tools"]
        except ValueError as e:
            if isinstance(e.args[0], dict) and e.args[0].get("code") == -32601:
                return []  # Server does not support tools
            raise

    def list_resource_templates(self):
        try:
            return self.jsonrpc("resources/templates/list")["resourceTemplates"]
        except ValueError as e:
            if isinstance(e.args[0], dict) and e.args[0].get("code") == -32601:
                return []  # Server does not support resource templates
            raise

    def list_resources(self):
        """
        https://modelcontextprotocol.io/specification/2024-11-05/server/resources#listing-resources
        """
        try:
            return self.jsonrpc("resources/list")["resources"] + self.list_resource_templates()
        except ValueError as e:
            if isinstance(e.args[0], dict) and e.args[0].get("code") == -32601:
                return []  # Server does not support resources
            raise

    def list_prompts(self):
        """
        https://modelcontextprotocol.io/specification/2025-03-26/server/prompts/#listing-prompts
        """
        try:
            return self.jsonrpc("prompts/list")["prompts"]
        except ValueError as e:
            if isinstance(e.args[0], dict) and e.args[0].get("code") == -32601:
                return []  # Server does not support resources
            raise

    def call_tool(self, name, arguments):
        """
        https://spec.modelcontextprotocol.io/specification/2024-11-05/server/tools/#calling-tools
        """
        result = self.jsonrpc("tools/call", {
            "name": name,
            "arguments": arguments
        })
        # Handle both direct content response and nested result structure
        if "content" in result:
            return result["content"]
        elif "result" in result and "content" in result["result"]:
            return result["result"]["content"]
        else:
            # If neither format, return the whole result
            return [{"type": "text", "text": str(result)}]

    def get_resource(self, uri):
        """
        https://modelcontextprotocol.io/specification/2024-11-05/server/resources/#getting-resources
        """
        return self.jsonrpc("resources/read", {
            "uri": uri
        })["contents"]

    def get_prompt(self, name, arguments):
        """
        https://modelcontextprotocol.io/specification/2025-03-26/server/prompts/#getting-prompts
        """
        return self.jsonrpc("prompts/get", {
            "name": name,
            "arguments": arguments
        })["messages"]

    def tool_call_example(arguments, result=None):
        if not "type" in arguments:
            if "anyOf" in arguments:
                return MCP.tool_call_example(arguments["anyOf"][0])
            else:
                return None
        if isinstance(arguments["type"], list):
            arguments["type"] = arguments["type"][0]

        if arguments["type"] == "string":
            result = ""
        elif arguments["type"] == "integer" or arguments["type"] == "number":
            result = 0
        elif arguments["type"] == "boolean":
            result = False
        elif arguments["type"] == "array":
            result = []
            if type(arguments["items"]) is dict:
                result.append(MCP.tool_call_example(arguments["items"]))
            else:
                for item in arguments["items"]:
                    result.append(MCP.tool_call_example(item))
        elif arguments["type"] == "object":
            result = {}
            if "properties" in arguments:
                for key, value in arguments["properties"].items():
                    if key not in arguments.get("required", []):
                        key += "?"
                    result[key] = MCP.tool_call_example(value)
        else:
            raise ValueError(f"Unsupported type: {arguments['type']}")

        return result


class SSEClient(MCP):
    def __init__(self, host: str, timeout: int = 10, verify: bool = True, token: str = None, proxies: dict = None, headers: dict = None, sse_path: str = '/sse', **kwargs):
        sse_headers = {"Accept": "application/json, text/event-stream"}
        if token:
            sse_headers["Authorization"] = f"Bearer {token}"
        if headers:
            sse_headers.update(headers)
        sse_url = host.rstrip('/') + '/' + (sse_path or '/sse').lstrip('/')
        self.sse = SSE(sse_url, timeout=timeout, verify=verify, headers=sse_headers, proxies=proxies)
        event, data = next(self.sse)
        assert event == "endpoint", f"Received {(event, data)}"
        self.messages_url = urljoin(host, data)

        super().__init__(host, timeout=timeout, verify=verify, token=token, proxies=proxies, headers=headers, **kwargs)

    def transmit(self, payload: dict, notification: bool = False) -> dict:
        r = self.session.post(self.messages_url, json=payload, timeout=self.timeout)
        assert r.ok, r.text

        if not notification:  # notifications don't have a response
            try:
                event, data = next(self.sse)
                return json.loads(data)
            except json.JSONDecodeError as e:
                print("JSON:", repr(data))
                raise


class StreamableHTTPClient(MCP):
    def __init__(self, host: str, **kwargs):
        # If we're using this client directly (bypassing SSE), we need to get the endpoint
        if kwargs.get('proxies'):
            # Try to get the endpoint from SSE without using proxy and keep SSE connection for responses
            temp_kwargs = kwargs.copy()
            temp_kwargs.pop('proxies', None)
            try:
                # Get a valid session_id from SSE (without proxy) and keep connection for responses
                sse_headers = {"Accept": "application/json, text/event-stream"}
                if temp_kwargs.get('token'):
                    sse_headers["Authorization"] = f"Bearer {temp_kwargs['token']}"
                if temp_kwargs.get('headers'):
                    sse_headers.update(temp_kwargs['headers'])
                sse_url = host.rstrip('/') + '/' + (temp_kwargs.get('sse_path', '/sse')).lstrip('/')
                
                # Make a direct request without proxy to get the endpoint and session_id
                import requests
                self.sse_response = requests.get(sse_url, headers=sse_headers, timeout=30, verify=temp_kwargs.get('verify', True), stream=True)
                if self.sse_response.ok:
                    # Read just the first few lines to get the endpoint, but keep connection alive
                    self.sse_iter = self.sse_response.iter_lines(decode_unicode=True)
                    for line in self.sse_iter:
                        if line and line.startswith('data: '):
                            endpoint_path = line[6:].strip()  # Remove 'data: '
                            self.messages_endpoint = urljoin(host, endpoint_path)
                            # Extract session_id from the endpoint path
                            from urllib.parse import urlparse, parse_qs
                            parsed = urlparse(endpoint_path)
                            if parsed.query and 'session_id' in parsed.query:
                                self.sid = parse_qs(parsed.query)['session_id'][0]
                            break
                    # Keep SSE connection alive for reading responses
            except Exception as e:
                # Fallback to default if anything fails
                import uuid
                self.sid = str(uuid.uuid4()).replace('-', '')
                self.messages_endpoint = f"{host.rstrip('/')}/messages/?session_id={self.sid}"
        
        super().__init__(host, **kwargs)
        
        # Set session ID in headers if we got it from SSE
        if hasattr(self, 'sid') and self.sid:
            self.session.headers["MCP-Session-ID"] = self.sid

    def __del__(self):
        # Clean up SSE connection if it exists
        if hasattr(self, 'sse_response'):
            try:
                self.sse_response.close()
            except:
                pass

    def transmit(self, payload: dict, notification: bool = False) -> dict:
        # Use the endpoint we discovered, or fall back to default behavior
        if hasattr(self, 'messages_endpoint'):
            endpoint = self.messages_endpoint
        else:
            # Prefer custom http endpoint if provided, otherwise default to /mcp
            endpoint = self.http_endpoint if getattr(self, 'http_endpoint', None) else (self.host.rstrip('/') + '/mcp')
        
        r = self.session.post(endpoint, json=payload, timeout=self.timeout)
        assert r.ok, r.text

        self.sid = r.headers.get("MCP-Session-ID")
        if self.sid:
            self.session.headers["MCP-Session-ID"] = self.sid

        if not notification:  # notifications don't have a response
            # If we got a 202 Accepted and have SSE connection, read real response
            if r.status_code == 202 and hasattr(self, 'sse_iter'):
                try:
                    # Read response from our persistent SSE connection
                    # We need to match the response with the request ID
                    request_id = payload.get('id')
                    timeout_counter = 0
                    max_timeout = 50  # Max lines to read before giving up
                    
                    for line in self.sse_iter:
                        timeout_counter += 1
                        if timeout_counter > max_timeout:
                            break
                            
                        if line and line.startswith('data: '):
                            response_data = line[6:].strip()
                            # Skip ping messages
                            if 'ping' in response_data:
                                continue
                            try:
                                data = json.loads(response_data)
                                # Check if this response matches our request
                                if request_id and data.get('id') == request_id:
                                    return data
                                elif not request_id and 'result' in data:
                                    # For notifications (no ID), return first valid result
                                    return data
                            except json.JSONDecodeError:
                                # If it's not JSON, might be a simple response
                                continue
                        elif line == '':
                            # Empty line indicates end of message, but continue reading
                            continue
                except Exception as e:
                    # If SSE reading fails, fall back to dummy response
                    pass
            
            content_type = r.headers.get("Content-Type", "").lower()
            if "text/event-stream" in content_type:
                sse = SSE.from_body(r.text)
                event, data = next(sse)
                data = json.loads(data)
            elif "application/json" in content_type:
                data = r.json()
            elif r.text.strip() and r.status_code != 202:
                # Try to parse as JSON even without proper Content-Type (but not for 202 Accepted)
                try:
                    data = json.loads(r.text)
                except json.JSONDecodeError:
                    raise ValueError(f"Could not parse response: {r.text}")
            else:
                # For 202 responses, return a dummy success response as fallback
                if r.status_code == 202:
                    # Return the expected structure for initialization response
                    if payload.get('method') == 'initialize':
                        return {
                            "result": {
                                "serverInfo": {
                                    "name": "MCP Server (via Proxy)",
                                    "version": "1.0.0"
                                }
                            }
                        }
                    else:
                        # For other methods, return generic success
                        return {
                            "result": {
                                "content": [{"type": "text", "text": "Success (via proxy)"}]
                            }
                        }
                else:
                    raise ValueError(
                        f"Unsupported Content-Type: {r.headers.get('Content-Type')}")

            return data


def get_mcp_info(host, **kwargs):
    """
    Get MCP server information.
    """
    try:
        mcp = MCP.new(host, **kwargs)
        tools = mcp.list_tools()
        resources = mcp.list_resources()
        prompts = mcp.list_prompts()
        return {
            "host": host,
            "server_info": mcp.server_info,
            "success": True,
            "tools": tools,
            "resources": resources,
            "prompts": prompts
        }
    except Exception as e:
        return {
            "host": host,
            "success": False,
            "error": str(e)
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Interact with a Model Context Protocol server")

    hosts_group = parser.add_mutually_exclusive_group(required=True)
    hosts_group.add_argument("host", nargs='?', type=str,
                             help="The host of the MCP server")
    hosts_group.add_argument("-f", "--file", type=Path,
                             help="File containing newline-separated hosts")
    parser.add_argument("-o", "--output", type=Path,
                        help="Output JSON file for the results")
    parser.add_argument("name_or_uri", type=str, nargs='?',
                        help="The name of the tool/prompt to call, or a resource URI")
    parser.add_argument("args", nargs='?', type=str,
                        help="Arguments for the tool call in JSON format")
    parser.add_argument("-r", "--raw", action="store_true",
                        help="Print raw JSON response")
    parser.add_argument("-t", "--timeout", type=int, default=10,
                        help="Timeout for requests in seconds, 0 for no timeout (default: %(default)s)")
    parser.add_argument("-T", "--threads", type=int, default=10,
                        help="Number of threads to use for concurrent requests (default: %(default)s)")
    parser.add_argument("-k", "--insecure", action="store_true",
                        help="Ignore SSL certificate errors")
    parser.add_argument("--token", type=str, default=os.environ.get("MCP_TOKEN"),
                        help="Bearer token for Authorization header (env MCP_TOKEN if unset)")
    parser.add_argument("--proxy", type=str, default=os.environ.get("HTTP_PROXY"),
                        help="HTTP(S) proxy URL, e.g. http://127.0.0.1:8080")
    parser.add_argument("--burp", action="store_true",
                        help="Shortcut for --proxy http://127.0.0.1:8080 and disable cert verify")
    parser.add_argument("-H", "--header", action="append", default=[], metavar="HEADER: VALUE",
                        help="Add custom request header; can be repeated. Example: -H 'X-Api-Key: abc'")
    parser.add_argument("--http-path", type=str, default=None,
                        help="Custom HTTP endpoint path (default: /mcp)")
    parser.add_argument("--sse-path", type=str, default="/sse",
                        help="Custom SSE endpoint path (default: /sse)")

    args = parser.parse_args()

    if args.file:
        hosts = args.file.read_text().splitlines()
    else:
        hosts = [args.host]
    hosts = ["http://" + host
             if not host.startswith("http") else host for host in hosts]
    if args.insecure or args.burp:
        requests.packages.urllib3.disable_warnings()
    if args.timeout == 0:
        args.timeout = None

    # Build proxies dict
    proxies = None
    if args.burp:
        proxy_url = "http://127.0.0.1:8080"
        proxies = {"http": proxy_url, "https": proxy_url}
        verify = False
    else:
        verify = not args.insecure
        if args.proxy:
            proxies = {"http": args.proxy, "https": args.proxy}

    # Parse custom headers
    extra_headers = {}
    for hv in args.header:
        if ":" not in hv:
            raise SystemExit(f"Invalid header format: {hv!r}. Use 'Name: Value'.")
        name, value = hv.split(":", 1)
        extra_headers[name.strip()] = value.strip()

    if not args.name_or_uri:
        # List tools/resources/prompts
        all_results = []
        with ThreadPoolExecutor(max_workers=args.threads) as executor:
            future_to_host = {executor.submit(get_mcp_info, host, timeout=args.timeout, verify=verify, token=args.token, proxies=proxies, headers=extra_headers, http_path=args.http_path, sse_path=args.sse_path): host
                              for host in hosts}

            for future in as_completed(future_to_host):
                host = future_to_host[future]
                data = future.result()
                all_results.append(data)
                # Header con estilo mejorado
                print(f"\n┌{'─'*78}┐")
                print(f"│ HOST: {host:<70} │")
                print(f"├{'─'*78}┤")
                
                if not data["success"]:
                    print(f"│ ✗ ERROR: {data['error']:<64} │")
                    print(f"└{'─'*78}┘")
                    continue

                # Información del servidor
                server_name = data["server_info"]["name"]
                print(f"│ Server Name: {server_name:<62} │")
                print(f"└{'─'*78}┘")
                print()

                # Tools section
                tools = data["tools"]
                if tools:
                    print(f"┌─ TOOLS ({len(tools)}) {'─'*(68-len(str(len(tools))))}┐")
                    if args.raw:
                        print(json.dumps(tools, indent=4))
                    else:
                        for i, tool in enumerate(tools, 1):
                            arguments = MCP.tool_call_example(tool['inputSchema'])
                            command = f"{tool['name']} '{json.dumps(arguments)}'"
                            description = tool.get('description', '')
                            
                            # Línea principal con descripción o comando
                            if description:
                                # Procesar toda la descripción sin truncar
                                desc_lines = []
                                
                                # Dividir por líneas existentes primero (preservar formato original)
                                original_lines = description.split('\n')
                                
                                for orig_line in original_lines:
                                    orig_line = orig_line.strip()
                                    if not orig_line:
                                        # Preservar líneas vacías como separadores
                                        desc_lines.append("")
                                        continue
                                        
                                    if len(orig_line) <= 72:
                                        desc_lines.append(orig_line)
                                    else:
                                        # Dividir líneas largas por palabras
                                        words = orig_line.split()
                                        current_line = ""
                                        for word in words:
                                            if len(current_line + " " + word) <= 72:
                                                current_line += (" " if current_line else "") + word
                                            else:
                                                if current_line:
                                                    desc_lines.append(current_line)
                                                current_line = word
                                        if current_line:
                                            desc_lines.append(current_line)
                                
                                # Mostrar todo sin truncar
                                if desc_lines:
                                    # Primera línea con número
                                    first_line = desc_lines[0] if desc_lines[0] else "(description continues below)"
                                    print(f"│ {i:>2}. {first_line:<72} │")
                                    
                                    # Resto de líneas de descripción
                                    for line in desc_lines[1:]:
                                        if line:  # Línea con contenido
                                            print(f"│     {line:<71} │")
                                        else:  # Línea vacía (separador)
                                            print(f"│{'':<76} │")
                                
                                # Línea separadora antes del comando
                                print(f"│     {'':<71} │")
                                
                                # Mostrar comando completo
                                if len(command) <= 70:
                                    print(f"│     → {command:<70} │")
                                else:
                                    # Para comandos muy largos, dividir en múltiples líneas
                                    cmd_parts = []
                                    remaining = command
                                    while remaining:
                                        if len(remaining) <= 70:
                                            cmd_parts.append(remaining)
                                            break
                                        else:
                                            # Buscar un buen punto de corte (después de una coma o espacio)
                                            cut_point = 67
                                            for char in [', ', ' ', '"', "'", '}', ']']:
                                                pos = remaining.rfind(char, 0, cut_point)
                                                if pos > 50:  # No cortar demasiado pronto
                                                    cut_point = pos + len(char)
                                                    break
                                            
                                            cmd_parts.append(remaining[:cut_point])
                                            remaining = remaining[cut_point:].lstrip()
                                    
                                    # Imprimir partes del comando
                                    for j, part in enumerate(cmd_parts):
                                        if j == 0:
                                            print(f"│     → {part:<70} │")
                                        else:
                                            print(f"│       {part:<69} │")
                            else:
                                # Solo comando, sin descripción
                                if len(command) <= 72:
                                    print(f"│ {i:>2}. {command:<72} │")
                                else:
                                    cmd_truncated = command[:69] + "..."
                                    print(f"│ {i:>2}. {cmd_truncated:<72} │")
                    print(f"└{'─'*78}┘")
                else:
                    print(f"┌─ TOOLS {'─'*70}┐")
                    print(f"│ No tools available{'':<56} │")
                    print(f"└{'─'*78}┘")
                
                print()

                # Resources section
                resources = data["resources"]
                if resources:
                    print(f"┌─ RESOURCES ({len(resources)}) {'─'*(65-len(str(len(resources))))}┐")
                    if args.raw:
                        print(json.dumps(resources, indent=4))
                    else:
                        for i, resource in enumerate(resources, 1):
                            if 'uriTemplate' in resource:
                                # Dynamic resource templates
                                template = resource['uriTemplate']
                                description = resource.get('description', '')
                                display_text = f"[TEMPLATE] {description or template}"
                                
                                # Truncar si es muy largo
                                if len(display_text) <= 72:
                                    print(f"│ {i:>2}. {display_text:<72} │")
                                else:
                                    truncated = display_text[:69] + "..."
                                    print(f"│ {i:>2}. {truncated:<72} │")
                                
                                if description and len(template) <= 70:
                                    print(f"│     → {template:<70} │")
                                elif description:
                                    template_truncated = template[:67] + "..."
                                    print(f"│     → {template_truncated:<70} │")
                            else:
                                # Static resources
                                name = resource.get('name', '')
                                uri = resource.get('uri', '')
                                description = resource.get('description', '')
                                
                                if name and name != uri:
                                    display_name = f"{name}"
                                    if description:
                                        display_name += f" ({description})"
                                    
                                    # Truncar si es muy largo
                                    if len(display_name) <= 72:
                                        print(f"│ {i:>2}. {display_name:<72} │")
                                    else:
                                        truncated = display_name[:69] + "..."
                                        print(f"│ {i:>2}. {truncated:<72} │")
                                    
                                    # URI
                                    if len(uri) <= 70:
                                        print(f"│     → {uri:<70} │")
                                    else:
                                        uri_truncated = uri[:67] + "..."
                                        print(f"│     → {uri_truncated:<70} │")
                                else:
                                    if description:
                                        if len(description) <= 72:
                                            print(f"│ {i:>2}. {description:<72} │")
                                        else:
                                            desc_truncated = description[:69] + "..."
                                            print(f"│ {i:>2}. {desc_truncated:<72} │")
                                        
                                        if len(uri) <= 70:
                                            print(f"│     → {uri:<70} │")
                                        else:
                                            uri_truncated = uri[:67] + "..."
                                            print(f"│     → {uri_truncated:<70} │")
                                    else:
                                        if len(uri) <= 72:
                                            print(f"│ {i:>2}. {uri:<72} │")
                                        else:
                                            uri_truncated = uri[:69] + "..."
                                            print(f"│ {i:>2}. {uri_truncated:<72} │")
                    print(f"└{'─'*78}┘")
                else:
                    print(f"┌─ RESOURCES {'─'*67}┐")
                    print(f"│ No resources available{'':<52} │")
                    print(f"└{'─'*78}┘")
                
                print()

                # Prompts section
                prompts = data["prompts"]
                if prompts:
                    print(f"┌─ PROMPTS ({len(prompts)}) {'─'*(67-len(str(len(prompts))))}┐")
                    if args.raw:
                        print(json.dumps(prompts, indent=4))
                    else:
                        for i, prompt in enumerate(prompts, 1):
                            arguments = {arg['name']: "" for arg in prompt.get('arguments', [])}
                            command = f"prompt/{prompt['name']} '{json.dumps(arguments)}'"
                            description = prompt.get('description', '')
                            
                            if description:
                                # Procesar toda la descripción sin truncar (igual que tools)
                                desc_lines = []
                                
                                # Dividir por líneas existentes primero (preservar formato original)
                                original_lines = description.split('\n')
                                
                                for orig_line in original_lines:
                                    orig_line = orig_line.strip()
                                    if not orig_line:
                                        # Preservar líneas vacías como separadores
                                        desc_lines.append("")
                                        continue
                                        
                                    if len(orig_line) <= 72:
                                        desc_lines.append(orig_line)
                                    else:
                                        # Dividir líneas largas por palabras
                                        words = orig_line.split()
                                        current_line = ""
                                        for word in words:
                                            if len(current_line + " " + word) <= 72:
                                                current_line += (" " if current_line else "") + word
                                            else:
                                                if current_line:
                                                    desc_lines.append(current_line)
                                                current_line = word
                                        if current_line:
                                            desc_lines.append(current_line)
                                
                                # Mostrar todo sin truncar
                                if desc_lines:
                                    # Primera línea con número
                                    first_line = desc_lines[0] if desc_lines[0] else "(description continues below)"
                                    print(f"│ {i:>2}. {first_line:<72} │")
                                    
                                    # Resto de líneas de descripción
                                    for line in desc_lines[1:]:
                                        if line:  # Línea con contenido
                                            print(f"│     {line:<71} │")
                                        else:  # Línea vacía (separador)
                                            print(f"│{'':<76} │")
                                
                                # Línea separadora antes del comando
                                print(f"│     {'':<71} │")
                                
                                # Mostrar comando completo
                                if len(command) <= 70:
                                    print(f"│     → {command:<70} │")
                                else:
                                    # Para comandos muy largos, dividir en múltiples líneas
                                    cmd_parts = []
                                    remaining = command
                                    while remaining:
                                        if len(remaining) <= 70:
                                            cmd_parts.append(remaining)
                                            break
                                        else:
                                            # Buscar un buen punto de corte
                                            cut_point = 67
                                            for char in [', ', ' ', '"', "'", '}', ']']:
                                                pos = remaining.rfind(char, 0, cut_point)
                                                if pos > 50:
                                                    cut_point = pos + len(char)
                                                    break
                                            
                                            cmd_parts.append(remaining[:cut_point])
                                            remaining = remaining[cut_point:].lstrip()
                                    
                                    # Imprimir partes del comando
                                    for j, part in enumerate(cmd_parts):
                                        if j == 0:
                                            print(f"│     → {part:<70} │")
                                        else:
                                            print(f"│       {part:<69} │")
                            else:
                                # Solo comando, sin descripción
                                if len(command) <= 72:
                                    print(f"│ {i:>2}. {command:<72} │")
                                else:
                                    cmd_truncated = command[:69] + "..."
                                    print(f"│ {i:>2}. {cmd_truncated:<72} │")
                    print(f"└{'─'*78}┘")
                else:
                    print(f"┌─ PROMPTS {'─'*69}┐")
                    print(f"│ No prompts available{'':<54} │")
                    print(f"└{'─'*78}┘")
                
                print()

        if args.output:
            with args.output.open("w") as f:
                json.dump(all_results, f, indent=4)
            print(f"Results written to {args.output}")
    else:
        # Call tool/prompt/resource
        mcp = MCP.new(hosts[0], timeout=args.timeout, verify=verify, token=args.token, proxies=proxies, headers=extra_headers, http_path=args.http_path, sse_path=args.sse_path)
        if "://" in args.name_or_uri:
            # Fetch resource
            result = mcp.get_resource(args.name_or_uri)
            if args.raw:
                print(json.dumps(result, indent=4))
            else:
                print(f"\n┌─ RESOURCE CONTENT {'─'*60}┐")
                for i, content in enumerate(result, 1):
                    extension = mimetypes.guess_extension(content["mimeType"])
                    mime_type = content.get("mimeType", "unknown")
                    
                    if 'blob' in content:
                        with open(tempfile.mktemp(suffix=extension), "wb") as f:
                            data = content.get("text", b64decode(content["blob"]))
                            f.write(b64decode(data))
                        print(f"│ [{i}] Binary Content{'':<56} │")
                        print(f"│     File: {f.name:<66} │")
                        print(f"│     Type: {mime_type:<66} │")
                        if i < len(result):
                            print(f"├{'─'*78}┤")
                    elif "text" in content:
                        print(f"│ [{i}] Text Content{'':<58} │")
                        print(f"│     Type: {mime_type:<66} │")
                        print(f"│     Content:{'':<64} │")
                        # Mostrar todo el contenido sin truncar
                        text_lines = content['text'].split('\n')
                        for line in text_lines:
                            if len(line) <= 71:
                                print(f"│     {line:<71} │")
                            else:
                                # Dividir líneas largas por palabras
                                words = line.split()
                                current_line = ""
                                for word in words:
                                    if len(current_line + " " + word) <= 71:
                                        current_line += (" " if current_line else "") + word
                                    else:
                                        if current_line:
                                            print(f"│     {current_line:<71} │")
                                        current_line = word
                                if current_line:
                                    print(f"│     {current_line:<71} │")
                        if i < len(result):
                            print(f"├{'─'*78}┤")
                    else:
                        print(f"│ [{i}] Unknown content type{'':<50} │")
                        if i < len(result):
                            print(f"├{'─'*78}┤")
                print(f"└{'─'*78}┘")
                print()
        elif args.name_or_uri.startswith("prompt/"):
            # Get prompt
            result = mcp.get_prompt(args.name_or_uri[7:],
                                    json.loads(args.args or "{}"))
            if args.raw:
                print(json.dumps(result, indent=4))
            else:
                print(f"\n┌─ PROMPT MESSAGES {'─'*62}┐")
                for i, message in enumerate(result, 1):
                    role = message.get("role", "unknown")
                    content = message["content"]
                    
                    print(f"│ [{i}] {role.upper()}{'':<{70-len(role)}} │")
                    print(f"├{'─'*78}┤")
                    
                    if content["type"] == "text":
                        print(f"│ Text Content:{'':<64} │")
                        # Mostrar todo el contenido sin truncar
                        text_lines = content['text'].split('\n')
                        for line in text_lines:
                            if len(line) <= 75:
                                print(f"│ {line:<75} │")
                            else:
                                # Dividir líneas largas por palabras
                                words = line.split()
                                current_line = ""
                                for word in words:
                                    if len(current_line + " " + word) <= 75:
                                        current_line += (" " if current_line else "") + word
                                    else:
                                        if current_line:
                                            print(f"│ {current_line:<75} │")
                                        current_line = word
                                if current_line:
                                    print(f"│ {current_line:<75} │")
                    elif content["type"] == "image" or content["type"] == "audio":
                        content_type = content["type"]
                        extension = mimetypes.guess_extension(content["mimeType"])
                        with open(tempfile.mktemp(suffix=extension), "wb") as f:
                            f.write(b64decode(content["data"]))
                        print(f"│ {content_type.title()} Content:{'':<{64-len(content_type)}} │")
                        print(f"│ File: {f.name:<70} │")
                        print(f"│ Type: {content.get('mimeType', 'unknown'):<70} │")
                    elif content["type"] == "resource":
                        resource = content["resource"]
                        print(f"│ Resource Content:{'':<58} │")
                        if 'text' in resource:
                            print(f"│ Text:{'':<71} │")
                            text_lines = resource['text'].split('\n')
                            for line in text_lines[:10]:
                                truncated_line = line[:74] + "..." if len(line) > 74 else line
                                print(f"│ {truncated_line:<75} │")
                            if len(text_lines) > 10:
                                print(f"│ ... ({len(text_lines)-10} more lines){'':<48} │")
                        elif 'blob' in resource:
                            extension = mimetypes.guess_extension(resource["mimeType"])
                            with open(tempfile.mktemp(suffix=extension), "wb") as f:
                                data = resource.get("text", b64decode(resource["blob"]))
                                f.write(b64decode(data))
                            print(f"│ File: {f.name:<70} │")
                            print(f"│ Type: {resource.get('mimeType', 'unknown'):<70} │")
                        else:
                            print(f"│ URI: {resource['uri']:<71} │")
                    else:
                        print(f"│ Unknown content type: {content['type']:<55} │")
                    
                    if i < len(result):
                        print(f"├{'─'*78}┤")
                print(f"└{'─'*78}┘")
                print()
        else:
            # Call tool
            result = mcp.call_tool(args.name_or_uri,
                                   json.loads(args.args or "{}"))
            if args.raw:
                print(json.dumps(result, indent=4))
            else:
                print(f"\n┌─ TOOL RESPONSE {'─'*63}┐")
                for i, content in enumerate(result, 1):
                    if content["type"] == "text":
                        text_content = content['text']
                        print(f"│ [{i}] Text Output{'':<60} │")
                        print(f"├{'─'*78}┤")
                        # Mostrar todo el contenido sin truncar
                        text_lines = text_content.split('\n')
                        for line in text_lines:
                            if len(line) <= 75:
                                print(f"│ {line:<75} │")
                            else:
                                # Dividir líneas largas por palabras
                                words = line.split()
                                current_line = ""
                                for word in words:
                                    if len(current_line + " " + word) <= 75:
                                        current_line += (" " if current_line else "") + word
                                    else:
                                        if current_line:
                                            print(f"│ {current_line:<75} │")
                                        current_line = word
                                if current_line:
                                    print(f"│ {current_line:<75} │")
                    elif content["type"] == "image" or content["type"] == "audio":
                        content_type = content["type"]
                        extension = mimetypes.guess_extension(content["mimeType"])
                        with open(tempfile.mktemp(suffix=extension), "wb") as f:
                            f.write(b64decode(content["data"]))
                        print(f"│ [{i}] {content_type.title()} Content{'':<{58-len(content_type)}} │")
                        print(f"├{'─'*78}┤")
                        print(f"│ File: {f.name:<70} │")
                        print(f"│ Type: {content.get('mimeType', 'unknown'):<70} │")
                    elif content["type"] == "resource":
                        resource = content["resource"]
                        print(f"│ [{i}] Resource Content{'':<54} │")
                        print(f"├{'─'*78}┤")
                        if 'text' in resource:
                            print(f"│ Text Content:{'':<64} │")
                            text_lines = resource['text'].split('\n')
                            for line in text_lines:
                                if len(line) <= 75:
                                    print(f"│ {line:<75} │")
                                else:
                                    # Dividir líneas largas por palabras
                                    words = line.split()
                                    current_line = ""
                                    for word in words:
                                        if len(current_line + " " + word) <= 75:
                                            current_line += (" " if current_line else "") + word
                                        else:
                                            if current_line:
                                                print(f"│ {current_line:<75} │")
                                            current_line = word
                                    if current_line:
                                        print(f"│ {current_line:<75} │")
                        elif 'blob' in resource:
                            extension = mimetypes.guess_extension(resource["mimeType"])
                            with open(tempfile.mktemp(suffix=extension), "wb") as f:
                                data = resource.get("text", b64decode(resource["blob"]))
                                f.write(b64decode(data))
                            print(f"│ File: {f.name:<70} │")
                            print(f"│ Type: {resource.get('mimeType', 'unknown'):<70} │")
                        else:
                            print(f"│ URI: {resource['uri']:<71} │")
                    else:
                        print(f"│ [{i}] Unknown content type: {content['type']:<49} │")
                    
                    if i < len(result):
                        print(f"├{'─'*78}┤")
                print(f"└{'─'*78}┘")
                print()

            if args.output:
                with args.output.open("w") as f:
                    json.dump(result, f, indent=4)
                print(f"Result written to {args.output}")

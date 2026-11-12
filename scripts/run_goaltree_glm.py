"""Run GoalTree with explicit GLM standard API configuration, never a demo fallback."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pico.config import _parse_env_line
from pico.providers.clients import OpenAICompatibleModelClient
from pico.web import Workbench, make_server
from pico.workspace_manager import WorkspaceManager

MODEL = 'glm-5.3-flash'
BASE_URL = 'https://open.bigmodel.cn/api/paas/v4'


def load_key(env_file):
    key = os.environ.get('BIGMODEL_API_KEY', '').strip()
    if not key and env_file.is_file():
        for number, line in enumerate(env_file.read_text(encoding='utf-8-sig').splitlines(), 1):
            try:
                setting = _parse_env_line(line)
            except ValueError:
                raise ValueError(f'Invalid local GLM configuration on line {number}') from None
            if setting and setting[0] == 'BIGMODEL_API_KEY':
                key = setting[1].strip()
    return key


def model_factory(key, counter=None, input_tokens=1032192, context_tokens=1048576):
    def create():
        client = OpenAICompatibleModelClient(MODEL, BASE_URL, key, temperature=1,
                                             timeout=180, api_format='chat_completions')
        client.top_p = .95
        client.max_transport_attempts = 2
        client.stream = True
        if counter is not None:
            client.context_counter = counter
            client.input_token_budget = input_tokens
            client.token_context_window = context_tokens
        return client
    return create


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT / 'artifacts/goaltree-workspace')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env.glm.local')
    parser.add_argument('--port', type=int, default=8773)
    parser.add_argument('--native-input-budget',type=int,default=33554432,
                        help='Independent serialized wire byte ceiling (default 32 MiB), NOT tokens')
    parser.add_argument('--native-context-window',type=int,default=67108864,
                        help='Legacy byte-mode ceiling; token mode uses --context-tokens')
    parser.add_argument('--context-tokens',type=int,default=1048576)
    parser.add_argument('--input-tokens',type=int,default=1032192)
    parser.add_argument('--prepare-context-tokenizer',action='store_true',help='Download only pinned official tokenizer assets, never model weights')
    parser.add_argument('--web-output-chars',type=int,default=120000,help='Cumulative web tool output characters per turn')
    parser.add_argument('--web-result-chars',type=int,default=12000,help='Maximum characters per web tool result')
    parser.add_argument('--web-read-lines',type=int,default=120,help='Maximum lines per cached document read')
    parser.add_argument('--registry',type=Path,help='Private registry of host-selected folders; defaults to the initial workspace .pico directory')
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--no-web-access', action='store_true', help='Disable public webpage search and reading')
    parser.add_argument('--search-backend',choices=('glm-free','exa-mcp','public'),default='glm-free',help='Default: GLM search_pro + free international search; no Tavily requests')
    parser.add_argument('--network-route', choices=('direct', 'environment'), default='direct',
                        help='Explicit transport route; direct avoids inherited proxy settings and keeps TLS verification')
    parser.add_argument('--check-config', action='store_true', help='Check local configuration without a model request')
    args = parser.parse_args(argv)
    from pico.context_tokens import GLMTokenCounter, prepare_tokenizer
    if args.prepare_context_tokenizer:
        prepare_tokenizer()
    try:
        counter = GLMTokenCounter()
        if not 1024 <= args.input_tokens < args.context_tokens <= 1048576:
            parser.error('GLM token limits must satisfy 1024 <= input < context <= 1048576')
    except (ImportError, ValueError) as exc:
        parser.error(str(exc) + '; install .[long-context] and run --prepare-context-tokenizer --check-config')
    key = load_key(args.env_file)
    if args.check_config:
        print(json.dumps({'model': MODEL, 'base_url': BASE_URL, 'configured': bool(key),
                          'api_format': 'chat_completions', 'model_calls': 0,
                          'token_context_window':args.context_tokens,'input_token_budget':args.input_tokens,
                          'tokenizer':counter.identity}))
        return 0 if key else 1
    if not key:
        parser.error('Set BIGMODEL_API_KEY in the local environment or .env.glm.local. No fallback provider is used.')
    # The existing secret-name redactor reads this environment variable. Never
    # place credentials in command arguments, public configuration or traces.
    os.environ['BIGMODEL_API_KEY'] = key
    args.workspace.mkdir(parents=True, exist_ok=True)
    # This launcher has already selected the explicit standard-API credential.
    # Do not let an ancestor .env overwrite it with a blank or another provider.
    app = Workbench(args.workspace, model_factory=model_factory(key,counter,args.input_tokens,args.context_tokens), harness_profile='workbench',load_env=False,
                    max_new_tokens=8192, shell_backend='appcontainer',
                    verification_command='python -B -m unittest discover -v', network_route=args.network_route,
                    native_input_budget=args.native_input_budget,native_context_window=args.native_context_window,
                    web_output_chars=args.web_output_chars,web_result_chars=args.web_result_chars,web_read_lines=args.web_read_lines,
                    web_access=not args.no_web_access,
                    search_backend=args.search_backend if not args.no_web_access else 'public',privacy_protection=True)
    if app.search_backend=='exa-mcp' and app.search_mcp:
        from pico.search_mcp import MCPError
        try:
            app.search_mcp.prepare(time.monotonic()+15)
            print(json.dumps({'search_mcp_warmup':app.search_mcp.config(),'requests':app.search_mcp.setup_requests}),flush=True)
        except MCPError as exc:
            print(json.dumps({'search_mcp_warmup':'failed','error':str(exc),'fallback':False}),flush=True)
    manager=WorkspaceManager(app,registry=args.registry)
    server = make_server(app, args.port,workspace_manager=manager)
    url = f'http://127.0.0.1:{server.server_port}/'
    print(json.dumps({'url': url, 'workspace': str(app.root), 'model': MODEL, 'base_url': BASE_URL,
                      'mode': 'live', 'shell_backend': 'appcontainer', 'max_new_tokens': 8192,
                      'stream': True, 'tool_stream': True,
                      'temperature': 1, 'top_p': .95, 'transport_attempts': 2,
                      'native_input_budget':args.native_input_budget,'native_context_window':args.native_context_window,
                      'web_output_limits':app.web_budget_options,
                      'native_counting': counter.counting, 'token_context_window':args.context_tokens,
                      'input_token_budget':args.input_tokens,
                      'planning_reasoning_effort':'low','implementation_reasoning_effort':'max',
                      'web_access':app.web_access, 'privacy_protection':app.privacy_store.enabled,
                      'search_backend':app.search_backend,
                      'workspace_registry':str(manager.path),
                      'network_route': args.network_route}, ensure_ascii=False), flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

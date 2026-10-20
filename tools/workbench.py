"""Start the authenticated local workbench; never print model credentials."""
import argparse
import json
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

def main():
    import uvicorn
    from autocoding.api import create_app
    from autocoding.host import Host
    from autocoding.projects import Projects
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=ROOT)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--reset-token', action='store_true',
                        help='replace the persistent local owner token before starting')
    args = parser.parse_args()
    storage = args.project.resolve() / '.local/workbench'
    host = Host(args.project, storage, config=ROOT / '.local/credentials/opencode.json',cold_start=True)
    token_file = storage / 'owner-token.txt'
    # A browser keeps this token in local storage.  Reusing it across restarts
    # prevents a healthy local service restart from logging its owner out.
    token = '' if args.reset_token or not token_file.is_file() else token_file.read_text(encoding='utf-8').strip()
    if len(token) < 32:
        token = secrets.token_urlsafe(32)
        token_file.write_text(token, encoding='utf-8')
    origin = f'http://127.0.0.1:{args.port}'
    app = create_app(host, token, origin=origin, ui=ROOT / 'ui/dist',registry=Projects(host))
    print('WORKBENCH:', origin)
    print('OWNER TOKEN FILE:', token_file)
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, ws_max_size=65536)

if __name__ == '__main__':
    main()

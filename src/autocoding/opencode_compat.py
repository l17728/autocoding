"""Session-local compatibility for OpenCode's undefined glob permission path."""
import json
from pathlib import Path


def isolate_profile(env, storage):
    child = dict(env)
    for name in ('OPENCODE_CONFIG', 'OPENCODE_CONFIG_DIR', 'OPENCODE_PURE'):
        child.pop(name, None)
    profile = Path(storage).resolve() / 'profile'
    profile.mkdir(parents=True,exist_ok=True)
    child.update(OPENCODE_TEST_HOME=str(profile), OPENCODE_DISABLE_PROJECT_CONFIG='true')
    return child


def configure_glob_path(env, storage, cwd):
    storage = Path(storage).resolve()
    storage.mkdir(parents=True, exist_ok=True)
    plugin = storage / 'glob-path-compat.mjs'
    marker = storage / 'glob-path-defaulted.json'
    # JSON literals preserve Windows paths without interpreting them as JS code.
    plugin.write_text('import {writeFile} from "node:fs/promises";\n'
        'export default async () => ({\n'
        '  "tool.execute.before": async (input, output) => {\n'
        '    if (input.tool !== "glob" || !output.args || output.args.path !== undefined) return;\n'
        '    output.args.path = ' + json.dumps(str(Path(cwd).resolve())) + ';\n'
        '    await writeFile(' + json.dumps(str(marker)) + ', JSON.stringify({\n'
        '      session_id: input.sessionID, call_id: input.callID, defaulted: true\n'
        '    }), "utf8");\n'
        '  }\n'
        '});\n', encoding='utf-8')
    config = json.loads(env.get('OPENCODE_CONFIG_CONTENT', '{}'))
    plugins = config.setdefault('plugin', [])
    uri = plugin.as_uri()
    if uri not in plugins:
        plugins.append(uri)
    # OpenCode 1.19's Global.Path.home reads this process-local override. XDG
    # directories are separately isolated by connect(). Never alter os.homedir
    # or the user's profile, and don't discover project/user external plugins.
    child = isolate_profile(env,storage)
    child['OPENCODE_CONFIG_CONTENT']=json.dumps(config)
    return child

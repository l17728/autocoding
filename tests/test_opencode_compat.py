import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.opencode_compat import configure_glob_path


class GlobCompatibilityTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required to exercise the actual OpenCode plugin')
    def test_missing_path_defaults_to_bound_workspace_without_changing_explicit_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = root / 'work tree'
            env = {'OPENCODE_CONFIG_CONTENT':json.dumps({'permission':{'*':'ask'},'plugin':['existing']}),
                   'OPENCODE_CONFIG':'external.json', 'OPENCODE_CONFIG_DIR':'external', 'OPENCODE_PURE':'1'}
            result = configure_glob_path(env, root/'private', cwd)
            result = configure_glob_path(result, root/'private', cwd)
            config = json.loads(result['OPENCODE_CONFIG_CONTENT'])
            self.assertEqual({'*':'ask'}, config['permission'])
            self.assertEqual(2, len(config['plugin']))
            self.assertEqual('true',result['OPENCODE_DISABLE_PROJECT_CONFIG'])
            self.assertEqual(str(root/'private/profile'),result['OPENCODE_TEST_HOME'])
            for name in ('OPENCODE_CONFIG','OPENCODE_CONFIG_DIR','OPENCODE_PURE'):
                self.assertNotIn(name,result)
                self.assertIn(name,env)
            self.assertEqual(['existing'], json.loads(env['OPENCODE_CONFIG_CONTENT'])['plugin'])
            script = '''import assert from 'node:assert/strict';
const plugin = (await import(process.argv[1])).default;
const hook = (await plugin())['tool.execute.before'];
const input = {tool:'glob',sessionID:'ses_test',callID:'call_test'};
const missing = {args:{pattern:'*.py'}};
await hook(input, missing);
assert.equal(missing.args.path, process.argv[2]);
for (const path of ['explicit', '', null]) {
  const output = {args:{pattern:'*.py',path}};
  await hook(input, output);
  assert.equal(output.args.path, path);
}
const other = {args:{pattern:'*.py'}};
await hook({...input,tool:'grep'},other);
assert.equal(other.args.path, undefined);
'''
            completed = subprocess.run(['node','--input-type=module','-e',script,config['plugin'][-1],str(cwd.resolve())],
                                       capture_output=True, timeout=15)
            self.assertEqual(0, completed.returncode, completed.stderr.decode(errors='replace'))
            marker = json.loads((root/'private/glob-path-defaulted.json').read_text())
            self.assertEqual({'session_id':'ses_test','call_id':'call_test','defaulted':True},marker)

"""Parity tests for browser-local planning and the Python planning model."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from dashboard import build_view, demo_snapshot


class OfflineTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node needed for JavaScript model validation')
    def test_planning_parity(self):
        cases = []
        for strategy in ('balanced', 'reset_first'):
            for reserve in (5, 8, 40):
                for disabled in ([], ['demo-4'], ['demo-2', 'demo-3', 'demo-4']):
                    cases.append({'packet':demo_snapshot(100), 'settings':{
                        'strategy':strategy, 'reserve':reserve, 'disabled':disabled}, 'now':100})
        cases.append({'packet':demo_snapshot(100), 'settings':{}, 'now':170})
        for age in (0,70):
            packet=demo_snapshot(100)
            packet['result']['accounts'][0]['usage']['primary']['resets_at']=300
            cases.append({'packet':packet,'settings':{},'now':100+age})
        script = """
require('./ui/offline.js');
let raw=''; process.stdin.on('data', x=>raw+=x);
process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(raw).map(c=>
  globalThis.RELAY_LOCAL.build(c.packet,c.settings,c.now,true)))));
"""
        output = subprocess.run(['node','-e',script], cwd=Path(__file__).parent,
                                input=json.dumps(cases), text=True, capture_output=True, check=True)
        actual = json.loads(output.stdout)
        for case, js in zip(cases, actual):
            py = build_view(case['packet'], case['settings'], now=case['now'], demo=True)
            for key in ('reset_wait_until','action','target','ranking','threshold','entry_threshold','stale','accounts'):
                self.assertEqual(py[key], js[key], (case['settings'],key))

    @unittest.skipUnless(shutil.which('node'), 'Node needed for JavaScript model validation')
    def test_offline_adapter_rejects_secret_and_mutation_endpoint(self):
        script = """
require('./ui/offline.js');
(async()=>{
 const adapter=globalThis.RELAY_LOCAL;
 const secret=await adapter.request('/api/import',{tokens:{access_token:'secret'}});
 if(secret.ok) process.exit(1);
 const switchResult=await adapter.request('/api/switch',{});
 if(switchResult.ok) process.exit(2);
 const view=await (await adapter.request('/api/state')).json();
 if(view.demo || view.execution_enabled || view.accounts.length) process.exit(3);
})();
"""
        subprocess.run(['node','-e',script], cwd=Path(__file__).parent, check=True)

    def test_standalone_has_no_external_assets_or_connections(self):
        html = (Path(__file__).parent/'Codex Relay.html').read_text()
        self.assertNotIn('src="http', html)
        self.assertNotIn('src="/app.js"', html)
        self.assertNotIn('href="/style.css"', html)
        self.assertIn("connect-src 'none'", html)
        self.assertIn('RELAY_LOCAL', html)


if __name__ == '__main__':
    unittest.main()

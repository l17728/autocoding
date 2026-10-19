import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir:'./e2e', timeout:60000, expect:{timeout:15000}, workers:1,
  use:{baseURL:'http://127.0.0.1:8767',channel:'msedge',viewport:{width:1500,height:950}},
  webServer:{command:'..\\.venv\\Scripts\\python.exe ..\\tools\\ui_test_server.py --port 8767',url:'http://127.0.0.1:8767/health',reuseExistingServer:false,timeout:process.env.AUTOCODING_NATIVE_TUI==='1'?180000:60000},
  outputDir:'../.local/playwright-results'
});

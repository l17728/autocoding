import {chromium, expect} from '@playwright/test';
import {readFileSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

const [origin, tokenFile, output] = process.argv.slice(2);
const report = {passed:false, simultaneous:false, sessions:[]};
let browser;
try {
  browser = await chromium.launch({channel:'msedge',headless:true});
  const page = await browser.newPage({viewport:{width:1900,height:1100}});
  let snapshot;
  page.on('response',async response=>{
    if(response.url().endsWith('/snapshot') && response.ok()) {
      try {snapshot=await response.json();} catch {}
    }
  });
  await page.goto(origin);
  await page.getByLabel('访问令牌',{exact:true}).fill(readFileSync(tokenFile,'utf8'));
  await page.getByRole('button',{name:'进入工作台'}).click();
  await page.getByLabel('执行 Agent',{exact:true}).selectOption('mixed');
  await page.getByRole('button',{name:'执行全部待办',exact:true}).click();
  await expect.poll(()=>snapshot?.runs?.filter(r=>r.state==='running').length,{timeout:180000}).toBe(3);
  const observed=snapshot;
  for(const name of ['Codex','OpenCode','Claude']) {
    const agent=name.toLowerCase();
    const session=observed.sessions.find(s=>s.agent===agent && observed.live_ids.includes(s.id));
    if(!session) throw new Error('MISSING_LIVE_SESSION');
    const card=page.locator('.agent-card').filter({has:page.getByRole('heading',{name,exact:true})});
    await expect(card).toBeVisible();
    await expect(card.locator('.mode')).toContainText(session.id.slice(-8));
    const box=await card.boundingBox();
    if(!box || box.x<0 || box.x+box.width>1900) throw new Error('CARD_OUTSIDE_VIEWPORT');
    report.sessions.push({agent,id:session.id});
  }
  report.simultaneous=true;
  await page.screenshot({path:join(output,'three-live-sessions.png'),fullPage:true});
  writeFileSync(join(output,'browser.json'),JSON.stringify(report,null,2));
  await expect.poll(()=>snapshot?.batches?.at(-1)?.state,{timeout:780000}).toMatch(/^(verified_candidate|rework_required|reconciling|failed)$/);
  report.batch_state=snapshot.batches.at(-1).state;
  if(report.batch_state!=='verified_candidate') throw new Error('BATCH_NOT_VERIFIED');
  await expect(page.getByText('集成候选已验证，尚未发布',{exact:true})).toBeVisible({timeout:780000});
  await page.screenshot({path:join(output,'verified-candidate.png'),fullPage:true});
  report.passed=true;
} catch(error) {
  // Never persist Playwright messages or DOM dumps: login fields may contain credentials.
  report.error=error.name;
} finally {
  await browser?.close();
  writeFileSync(join(output,'browser.json'),JSON.stringify(report,null,2));
}
process.exitCode=report.passed?0:1;

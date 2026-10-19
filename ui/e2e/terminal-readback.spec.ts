import {test,expect} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';

for(const scenario of ['verified','mismatch','write-only','stale-session'])test('folded composer requires exact readback: '+scenario,async({page})=>{
  const value=('中文😀  exact spaces\n'.repeat(500))+'END';
  const session={id:'terminal_readback',agent:'codex',owner:'human',epoch:0,state:'ready',live:true,can_resume:false};
  let started=false;
  const keys:string[]=[];
  await page.route('**/terminals**',async route=>{
    const request=route.request(),path=new URL(request.url()).pathname;
    if(path.endsWith('/terminals')){
      if(request.method()==='POST')started=true;
      await route.fulfill({json:request.method()==='POST'?session:started?[session]:[]});
    }else if(path.endsWith('/paste')){
      expect(request.postDataJSON().text).toBe(value);
      if(scenario==='stale-session'){
        session.epoch=1;
        // Let the existing status poll observe takeover before the receipt.
        await page.waitForTimeout(1800);
      }
      await route.fulfill({json:{state:'sent_unconfirmed',draft_confirmation:{
        method:scenario==='write-only'?'editor_write':'editor_readback',
        sha256:createHash('sha256').update(scenario==='mismatch'?value.slice(0,-1):value).digest('hex')
      }}});
    }else{
      if(path.endsWith('/key'))keys.push(request.postDataJSON().name);
      await route.fulfill({json:{state:'sent_unconfirmed'}});
    }
  });
  await page.routeWebSocket('**/terminals/*/stream',socket=>{
    socket.onMessage(()=>socket.send(JSON.stringify({frames:[{seq:1,replay:true,text:'READY\r\n› [Pasted Content 9503 chars]'}]})));
  });
  await page.goto('/');
  await page.getByLabel('访问令牌',{exact:true}).fill(readFileSync('../.local/ui-e2e/.local/workbench/owner-token.txt','utf8'));
  await page.getByRole('button',{name:'进入工作台'}).click();
  await page.getByRole('button',{name:'打开本机终端',exact:true}).click();
  await page.getByRole('button',{name:'启动终端',exact:true}).click();
  await page.getByTestId('terminal-screen').scrollIntoViewIfNeeded();
  await expect(page.locator('.xterm-rows')).toContainText('Pasted Content');
  await page.getByLabel('终端输入',{exact:true}).fill(value);
  await page.getByRole('button',{name:'发送到终端',exact:true}).click();
  if(scenario==='verified'){
    await expect.poll(()=>keys).toEqual(['enter']);
    await expect(page.locator('.terminal-panel')).toContainText('输入已写入，尚未确认执行。');
  }else{
    await expect(page.locator('.terminal-panel')).toContainText(scenario==='stale-session'?'会话已改变':'草稿读回校验不一致');
    expect(keys).toEqual([]);
  }
  await page.waitForTimeout(700);
  expect(keys).toEqual(scenario==='verified'?['enter']:[]);
});

import {test,expect} from '@playwright/test';
import {readFileSync} from 'node:fs';

test('creation failure exposes safe stage and explicit bounded restart',async({page})=>{
  const failed={id:'terminal_failed',agent:'codex',owner:'human',epoch:0,state:'closed',live:false,can_resume:true,
    startup_state:'failed',startup_attempt:3,startup_limit:3,startup_error:'CONPTY_START_FAILED',
    startup_diagnostic:{stage:'input_connect',kind:'TimeoutError',stopped_confirmed:true}};
  let restarted=false;
  await page.route('**/terminals**',async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith('/terminals')){
      if(route.request().method()==='POST'){
        expect(route.request().postDataJSON()).toMatchObject({agent:'codex',resume_from:'terminal_failed',startup_attempts:3});
        restarted=true;
        await route.fulfill({json:{...failed,id:'terminal_again',can_resume:false,startup_state:'starting'}});
      }else await route.fulfill({json:restarted?[failed,{...failed,id:'terminal_again',can_resume:false,startup_state:'starting'}]:[failed]});
    }else await route.fulfill({json:{}});
  });
  await page.routeWebSocket('**/terminals/*/stream',socket=>socket.onMessage(()=>socket.send(JSON.stringify({frames:[],closed:true}))));
  await page.goto('/');
  await page.getByLabel('访问令牌',{exact:true}).fill(readFileSync('../.local/ui-e2e/.local/workbench/owner-token.txt','utf8'));
  await page.getByRole('button',{name:'进入工作台'}).click();
  await page.getByRole('button',{name:'打开本机终端',exact:true}).click();
  await page.getByLabel('终端会话',{exact:true}).selectOption('terminal_failed');
  await expect(page.locator('.terminal-panel')).toContainText('连接输入通道 · TimeoutError · 已确认资源清理');
  await expect(page.locator('.terminal-panel')).toContainText('第 3/3 次');
  await page.getByRole('button',{name:'再次启动此工作区',exact:true}).click();
  await expect.poll(()=>restarted).toBe(true);
  await expect(page.getByLabel('终端会话',{exact:true})).toHaveValue('terminal_again');
});

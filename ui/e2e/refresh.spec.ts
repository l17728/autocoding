import {test,expect} from '@playwright/test';
import {readFileSync} from 'node:fs';

test('refresh ignores older snapshots, deduplicates output and reports connection loss',async({page})=>{
  let snapshots=0,offline=false;
  let releaseOld:()=>void=()=>{};
  const oldResponse=new Promise<void>(resolve=>{releaseOld=resolve});
  const state=(description:string)=>({tasks:[{id:'task_refresh',description,state:'done',attempt:1}],
    sessions:[{id:'session_refresh',agent:'claude',state:'ready',owner:'human',epoch:1}],
    approvals:[],latest_seq:1,paused:false,live_ids:['session_refresh'],runs:[],questions:[],batches:[]});
  await page.route('**/snapshot',async route=>{
    if(offline){await route.fulfill({status:503,contentType:'text/html',body:'service unavailable'});return}
    const first=++snapshots===1;if(first)await oldResponse;
    await route.fulfill({json:state(first?'过时任务快照':'最新任务快照')});
  });
  await page.route('**/output?*',route=>route.fulfill({json:{latest_seq:1,events:[{seq:1,session_id:'session_refresh',type:'text',text:'唯一输出记录'}]}}));
  await page.route('**/pause',route=>route.fulfill({json:{paused:false}}));
  await page.goto('/');
  await page.getByLabel('访问令牌',{exact:true}).fill(readFileSync('../.local/ui-e2e/.local/workbench/owner-token.txt','utf-8'));
  await page.getByRole('button',{name:'进入工作台'}).click();
  await expect.poll(()=>snapshots).toBe(1);
  await page.getByRole('button',{name:'暂停派发',exact:true}).click();
  await expect(page.getByText('最新任务快照',{exact:true})).toBeVisible();
  releaseOld();
  await expect.poll(()=>snapshots).toBeGreaterThanOrEqual(3);
  await expect(page.getByText('过时任务快照',{exact:true})).toHaveCount(0);
  await expect(page.getByText('唯一输出记录',{exact:true})).toHaveCount(1);
  offline=true;
  await expect(page.getByText('服务未连接或正在重连',{exact:true})).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('服务响应无法读取（HTTP 503）');
});

test('switching A to B to A rejects responses from the previous visit',async({page})=>{
  const a={id:'project_a',name:'项目 A',root:'D:/fixture-a'},b={id:'project_b',name:'项目 B',root:'D:/fixture-b'};
  let first=true,oldDelivered=false,release:()=>void=()=>{};
  const delayed=new Promise<void>(resolve=>{release=resolve});
  await page.route('**/api/v1/**',async route=>{
    const path=new URL(route.request().url()).pathname;
    const project=path.includes('project_b')?b:a;
    let json:unknown={};
    if(path.endsWith('/project'))json=a;
    else if(path.endsWith('/projects'))json=[a,b];
    else if(path.endsWith('/settings'))json={...project,revision:1};
    else if(path.endsWith('/snapshot')){
      const stale=project.id===a.id&&first;
      if(stale){first=false;await delayed;oldDelivered=true}
      json={tasks:[{id:'task',description:stale?'上次访问的旧快照':project.name+' 当前快照',state:'done',attempt:1}],sessions:[],approvals:[],latest_seq:1,paused:false,live_ids:[],runs:[],questions:[],batches:[]};
    }else if(path.endsWith('/output'))json={latest_seq:1,events:[]};
    else if(path.endsWith('/reviews'))json={settings:{enabled:true,interval_minutes:60},reviews:[],principles:[],knowledge:[],knowledge_total:0,health:{}};
    else if(path.endsWith('/interactions'))json={total:0,requests:0,owner_answers:0,repeat_requests:0,pending:0,items:[],notice:''};
    else if(path.endsWith('/closures')||path.endsWith('/terminals'))json=[];
    await route.fulfill({json});
  });
  await page.goto('/');await page.getByLabel('访问令牌',{exact:true}).fill('fixture-token');
  await page.getByRole('button',{name:'进入工作台'}).click();
  await page.locator('.project-panel > summary').click();
  await page.getByLabel('切换项目',{exact:true}).selectOption(b.id);
  await expect(page.getByText('项目 B 当前快照',{exact:true})).toBeVisible();
  await page.getByLabel('切换项目',{exact:true}).selectOption(a.id);
  await expect(page.getByText('项目 A 当前快照',{exact:true})).toBeVisible();
  release();
  await expect.poll(()=>oldDelivered).toBe(true);
  await expect(page.getByText('上次访问的旧快照',{exact:true})).toHaveCount(0);
  await expect(page.getByText('项目 A 当前快照',{exact:true})).toBeVisible();
});

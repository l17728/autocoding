import {test,expect} from '@playwright/test';

test('paged tasks, coverage evidence and expert dispositions remain explicit',async({page})=>{
  let dismissed=false,requirements:any[]=[],saved:any,modelCalls=0;
  const project={id:'project_quality',name:'质量检查',root:'D:/fixture'};
  await page.route('**/api/v1/**',async route=>{
    const url=new URL(route.request().url()),path=url.pathname;let json:any={};
    if(path.endsWith('/project'))json=project;
    else if(path.endsWith('/projects'))json=[project];
    else if(path.endsWith('/settings'))json={...project,revision:1};
    else if(path.endsWith('/snapshot')){
      const offset=Number(url.searchParams.get('task_offset')||0);
      json={tasks:[{id:offset?'task_last':'task_first',description:offset?'最后一页任务':'第一页任务',state:'todo',attempt:0}],sessions:[],approvals:[],runs:[],questions:[],batches:[],latest_seq:1,paused:false,live_ids:[],totals:{tasks:101},task_page:{offset,total:101,next_offset:offset?null:100}};
    }else if(path.endsWith('/output'))json={events:[],latest_seq:0};
    else if(path.endsWith('/reviews'))json={settings:{enabled:false,interval_minutes:60},reviews:[],principles:[],knowledge:[],knowledge_total:0,health:{}};
    else if(path.endsWith('/interactions'))json={items:[],total:0};
    else if(path.endsWith('/closures')||path.endsWith('/terminals'))json=[];
    else if(path.endsWith('/learning-evaluations'))json={suite:{cases:[]},fingerprint:'b'.repeat(64),reports:[]};
    else if(path.endsWith('/learning-evaluations/history-cases'))json={cases:[{id:'history_example',scope:'inside',prompt:'Return JSON answers',expected:{answers:[['JSON']]}}],notice:'历史复现不是未见过的保留评测集。'};
    else if(path.endsWith('/learning-evaluations/run')){modelCalls++;json={};}
    else if(path.endsWith('/coverage')){
      if(route.request().method()==='POST'){saved=route.request().postDataJSON();requirements=saved.requirements}
      json={fingerprint:'a'.repeat(64),revision:1,requirements,unmapped_tasks:[],matrix:requirements.map(r=>({...r,status:'unproven',cases:r.cases.map((c:any)=>({...c,status:'unproven',artifact_sha256:null}))}))};
    }else if(path.includes('/findings/')){dismissed=true;json={state:'dismissed'}}
    else if(path.endsWith('/expert-reviews'))json={settings:{enabled:true},reports:[{id:'review',state:'completed',current:true,candidate_sha:'abc123',reviews:[],findings:[{id:'finding',severity:'P2',title:'边界遗漏',location:'a.py:1',recommendation:'补充边界测试',state:dismissed?'dismissed':'open'}]}]};
    await route.fulfill({json});
  });
  await page.goto('/');await page.getByLabel('访问令牌',{exact:true}).fill('fixture');await page.getByRole('button',{name:'进入工作台'}).click();
  await expect(page.getByRole('button',{name:'执行本页待办',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'下一页任务',exact:true}).click();
  await expect(page.getByText('最后一页任务',{exact:true})).toBeVisible();
  await expect(page.getByText('第一页任务',{exact:true})).toHaveCount(0);
  await page.getByText('需求与验收覆盖矩阵',{exact:true}).click();
  await page.getByRole('button',{name:'载入覆盖矩阵'}).click();
  await page.getByLabel('需求 JSON',{exact:true}).fill(JSON.stringify([{id:'R1',description:'边界需求',cases:[{task_id:'task_last',criterion:'unit'}]}]));
  await page.getByRole('button',{name:'保存需求映射'}).click();
  await expect(page.getByText('边界需求 · 证据不完备',{exact:true})).toBeVisible();
  expect(saved.requirements[0].cases[0].task_id).toBe('task_last');
  await page.getByText('独立专家审查',{exact:true}).click();await page.getByRole('button',{name:'载入审查记录'}).click();
  await page.getByLabel('处理依据',{exact:true}).fill('已有防护且完成核对');
  await page.getByRole('button',{name:'记录不采用理由'}).click();
  await expect(page.getByText('dismissed',{exact:true})).toBeVisible();
  await page.getByText('知识效果对照评测',{exact:true}).click();
  await page.getByRole('button',{name:'载入评测',exact:true}).click();
  await page.getByRole('button',{name:'从用户历史答案生成草稿（替换当前草稿，不调用模型）',exact:true}).click();
  await expect(page.getByLabel('场景 JSON',{exact:true})).toHaveValue(/history_example/);
  await expect(page.getByText(/历史复现不是未见过的保留评测集/)).toBeVisible();
  expect(modelCalls).toBe(0);
});

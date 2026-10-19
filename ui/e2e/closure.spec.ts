import {test,expect} from '@playwright/test';
import {readFileSync} from 'node:fs';

test('project closure report is searchable and shows evidence and lessons',async({page})=>{
  // Browser rendering fixture; backend persistence and completion gates have
  // separate tests using the actual SQLite ledger.
  await page.route('**/pm/closures',route=>route.fulfill({json:[{
    id:'closure_fixture',created_at:1792000000,summary:'项目登记任务均已验证；尚未发布。',task_snapshot_current:false,
    candidate_sha:'a'.repeat(40),integration_sha256:'b'.repeat(64),
    tasks:[{id:'task_search',description:'修复项目搜索',state:'done'}],
    bugfix_review:[{run_id:'run_failed',task_id:'task_search',state:'rework_required',failed_checks:['search_regression'],later_verified:true}],
    patterns:{search_regression:1},lessons:[{content:'搜索修复后必须回归空输入与无结果场景。'}],
    limitations:['登记检查不能证明所有用例完备。'],
    interaction_summary:{recorded_questions:2,help_requests:1,owner_answers:1}
  }]}));
  await page.goto('/');
  await page.getByLabel('访问令牌',{exact:true}).fill(readFileSync('../.local/ui-e2e/.local/workbench/owner-token.txt','utf-8'));
  await page.getByRole('button',{name:'进入工作台'}).click();
  await page.getByText('项目收尾复盘与报告',{exact:true}).click();
  await expect(page.getByText('项目登记任务均已验证；尚未发布。',{exact:true})).toBeVisible();
  await expect(page.getByText('历史快照，项目已变化',{exact:true})).toBeVisible();
  await expect(page.getByText('搜索修复后必须回归空输入与无结果场景。',{exact:true})).toBeVisible();
  await page.getByText('返工与 bugfix 总结 · 1 条',{exact:true}).click();
  await expect(page.getByText(/task_search：search_regression；后来有同任务验收通过记录/)).toBeVisible();
  await page.getByLabel('查找收尾报告').fill('没有这个关键词');
  await expect(page.getByText('项目登记任务均已验证；尚未发布。',{exact:true})).toHaveCount(0);
  await page.getByLabel('查找收尾报告').fill('搜索');
  await expect(page.getByText('项目登记任务均已验证；尚未发布。',{exact:true})).toBeVisible();
  await page.locator('.closure').screenshot({path:'../.local/browser-project-closure.png'});
});

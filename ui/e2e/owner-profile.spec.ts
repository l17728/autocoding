import {test,expect} from '@playwright/test';
import {readFileSync} from 'node:fs';

test('Owner working preferences persist and explicitly reload over a draft',async({page})=>{
  await page.goto('/');
  await page.getByLabel('访问令牌',{exact:true}).fill(readFileSync('../.local/ui-e2e/.local/workbench/owner-token.txt','utf8'));
  await page.getByRole('button',{name:'进入工作台'}).click();
  await page.locator('.reflection > summary').click();
  const style=page.getByRole('textbox',{name:'沟通风格',exact:true});
  await expect(style).toBeVisible();await style.fill('先给验收结果，再列证据和剩余项。');
  await page.getByRole('button',{name:'保存工作偏好',exact:true}).click();
  await expect(page.getByRole('status')).toContainText('工作偏好已保存');
  await style.fill('未保存草稿');
  await page.getByRole('button',{name:'加载最新偏好（替换草稿）',exact:true}).click();
  await expect(style).toHaveValue('先给验收结果，再列证据和剩余项。');
});

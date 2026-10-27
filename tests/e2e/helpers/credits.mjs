import assert from 'node:assert/strict';

/** Authorize the server quote before starting a new generation. Never used for stop clicks. */
export async function confirmGeneration(page, {timeout = 20000} = {}) {
  await page.locator('#sendButton').click();
  await page.waitForFunction(() => document.querySelector('#sendButton')?.title === '确认发送', null, {timeout});
  assert.match(await page.locator('#quoteSummary').innerText(), /本轮最多.+积分/);
  assert.notEqual(await page.locator('#question').inputValue(), '', 'draft stays until authorization');
  await page.locator('#sendButton').click();
}

/** Ordinary auth uses the server challenge; administrator auth additionally retains its math fixture. */
export async function readyAuth(page) {
  if (await page.locator('#math').count()) {
    await page.waitForFunction(() => document.querySelector('#math')?.src.startsWith('data:'));
    const src = await page.locator('#math').getAttribute('src');
    const match = Buffer.from(src.split(',')[1], 'base64').toString().match(/>(\d+) ([+−\-×]) (\d+) = \?<\/text>/);
    assert.ok(match, 'isolated administrator captcha is loaded');
    const a=+match[1], b=+match[3];
    await page.locator('#mathAnswer').fill(String(match[2]==='+'?a+b:match[2]==='×'?a*b:a-b));
  } else {
    await page.waitForFunction(() => !document.querySelector('#submit')?.disabled);
    assert.match(await page.locator('#verificationStatus').innerText(), /提交时进行安全验证/);
    assert.equal(await page.locator('#mathAnswer').count(), 0, 'ordinary auth has no duplicate manual verification');
  }
}

/** Logout must revoke account access, clear its workspace and return a bounded guest space. */
export async function guestLogout(page, base) {
  await page.locator('#logout').click();
  await page.waitForFunction(() => document.querySelector('#accountName')?.textContent.includes('访客') && !document.querySelector('#question')?.disabled);
  assert.equal(new URL(page.url()).pathname, '/app');
  const me = await page.request.get(base+'/api/auth/me').then(r=>r.json());
  assert.equal(me.user?.is_guest, true);
  assert.equal(await page.locator('.message').count(), 0, 'account transcript cleared after logout');
  assert.equal(await page.locator('.history-item').count(), 0, 'account history hidden after logout');
}

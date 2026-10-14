// Optional browser smoke test: node tests/browser_smoke.mjs
// Uses an installed Chrome and Node 22+, with a temporary demo database.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, writeFile, rm, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const temporary = await mkdtemp(path.join(tmpdir(), 'smartlib-browser-'));
const output = path.join(root, 'artifacts');
const chromePath = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const port = process.env.SMARTLIB_TEST_PORT || '5057';
const url = `http://127.0.0.1:${port}`;
let server, chrome, socket;
let nextId = 1;
const pending = new Map();
const errors = [];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function until(check, label, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try { if (await check()) return; } catch { /* Browser/server booting. */ }
    await sleep(100);
  }
  throw new Error(`Timed out: ${label}`);
}
function command(method, params = {}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const result = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || 'Browser evaluation failed');
  return result.result.value;
}
async function waitFor(expression, label) {
  await until(() => evaluate(expression), label);
}
async function screenshot(name) {
  const { data } = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  await writeFile(path.join(output, `${name}.png`), Buffer.from(data, 'base64'));
}
async function route(name) {
  await evaluate(`location.hash = ${JSON.stringify('#' + name)};window.SmartLibUI.renderRoute()`);
  await waitFor("document.querySelector('#main-content .page-header') && !document.querySelector('#main-content .content-loading')", `${name} page`);
}
async function click(selector) {
  await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
}
async function fill(form, fields) {
  await evaluate(`(() => {const form=document.querySelector(${JSON.stringify(form)});const fields=${JSON.stringify(fields)};for(const [key,value] of Object.entries(fields)){const input=form.elements.namedItem(key);if(!input)throw new Error('Missing field: '+key);input.value=String(value);input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));}})()`);
}
async function submitModal() {
  await evaluate("document.querySelector('#modal-body form').requestSubmit()");
  await until(async () => {
    const error = await evaluate("document.querySelector('#modal-body .form-error:not([hidden])')?.textContent || ''");
    if (error) throw new Error(error);
    return evaluate("document.querySelector('#modal-layer').hidden");
  }, 'Modal submission');
  await waitFor("!document.querySelector('#main-content .content-loading')", 'Refresh after modal');
}

try {
  assert(existsSync(chromePath), 'Set CHROME_PATH to an installed Chrome executable.');
  await mkdir(output, { recursive: true });
  server = spawn(process.env.PYTHON || 'python', ['app.py'], {
    cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, SMARTLIB_DATABASE: path.join(temporary, 'library.db'), SMARTLIB_PORT: port,
      SMARTLIB_HOST: '127.0.0.1', SMARTLIB_SEED_DEMO: '1', SMARTLIB_ADMIN_EMAIL: 'admin@smartlib.local',
      SMARTLIB_ADMIN_PASSWORD: 'Admin@12345', SMARTLIB_SECRET_KEY: 'browser-smoke-test-key', SMARTLIB_SECURE_COOKIES: '0' }
  });
  await until(async () => (await fetch(url)).ok, 'Flask startup');
  const profile = path.join(temporary, 'chrome-profile');
  chrome = spawn(chromePath, ['--headless=new', '--no-first-run', '--no-default-browser-check',
    '--disable-gpu', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'],
    { windowsHide: true, stdio: 'ignore' });
  let debugPort;
  await until(async () => {
    debugPort = (await readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0];
    return Boolean(debugPort);
  }, 'Chrome startup');
  const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
  const tab = targets.find(target => target.type === 'page');
  socket = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (process.env.SMARTLIB_BROWSER_DEBUG) console.log('CDP response', message.id || message.method);
    if (message.id && pending.has(message.id)) {
      const task = pending.get(message.id);
      clearTimeout(task.timer); pending.delete(message.id);
      message.error ? task.reject(new Error(message.error.message)) : task.resolve(message.result);
    } else if (message.method === 'Runtime.exceptionThrown') {
      errors.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
    } else if (message.method === 'Log.entryAdded' && message.params.entry.level === 'error') {
      errors.push(message.params.entry.text);
    }
  };
  if (process.env.SMARTLIB_BROWSER_DEBUG) console.log('Connected to Chrome page:',tab.url,tab.webSocketDebuggerUrl);
  if (process.env.SMARTLIB_BROWSER_DEBUG) console.log('Chrome version:',await command('Browser.getVersion'));
  await command('Log.enable');
  await command('Page.enable');
  await command('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await command('Page.navigate', { url });
  await command('Runtime.enable');
  await waitFor("document.querySelector('#auth-screen') && !document.querySelector('#auth-screen').hidden", 'Login screen');
  await screenshot('login');
  await evaluate("document.querySelector('[data-demo=admin]').click();document.querySelector('#login-form').requestSubmit()");
  await waitFor("!document.querySelector('#app-shell').hidden && document.querySelector('#main-content .page-header')", 'Admin dashboard');
  await screenshot('admin-dashboard');
  for (const route of ['catalog', 'loans', 'users', 'settings', 'profile']) {
    await evaluate(`location.hash = ${JSON.stringify('#' + route)}`);
    await sleep(400);
    await waitFor("!document.querySelector('#main-content .content-loading')", `${route} rendering`);
    const content = await evaluate("document.querySelector('#main-content').innerText");
    assert(content.length > 50, `${route} has content`);
  }
  await route('catalog');
  await screenshot('admin-catalog');

  // Exercise real form handlers, ownership transitions, and file upload/download.
  await click('[data-action="book-add"]');
  await fill('#modal-body form',{title:'Browser Test Book',author:'Browser Test Author',category:'Technology',total_copies:2,description:'Created through the admin panel.'});
  await submitModal();
  const bookId = await evaluate("window.SmartLibUI.state.books.find(book=>book.title==='Browser Test Book').id");
  await route('users');
  await click('[data-action="user-add"]');
  await fill('#modal-body form',{name:'Browser Test Student',email:'browser@example.test',password:'Browser@12345',student_id:'BROWSER-001',department:'Computer Science',role:'student'});
  await submitModal();
  const studentId = await evaluate("window.SmartLibUI.state.users.find(user=>user.email==='browser@example.test').id");
  await route('catalog');
  await click(`[data-action="borrow"][data-id="${bookId}"]`);
  await waitFor("document.querySelector('#modal-body [name=user_id]')", 'Admin issue form');
  await fill('#modal-body form',{user_id:studentId});
  await submitModal();
  await route('loans');
  const loanId = await evaluate(`window.SmartLibUI.state.loans.find(loan=>loan.book_id===${bookId} && loan.user_id===${studentId}).id`);
  await click(`[data-action="return"][data-id="${loanId}"]`);
  await submitModal();
  assert(await evaluate(`window.SmartLibUI.state.loans.find(loan=>loan.id===${loanId}).status==='returned'`),'Admin return recorded');
  await click('[data-action="pay"]');
  await submitModal();
  await route('catalog');
  await click(`[data-action="book-edit"][data-id="${bookId}"]`);
  await fill('#modal-body form',{title:'Browser Test Book Updated'});
  await submitModal();
  await click(`[data-action="book-archive"][data-id="${bookId}"]`);
  await submitModal();
  assert(await evaluate(`!window.SmartLibUI.state.books.some(book=>book.id===${bookId})`),'Archived title removed');
  await route('settings');
  await fill('#policy-form',{loan_days:21,max_loans:4,fine_per_day:'4.25'});
  await evaluate("document.querySelector('#policy-form').requestSubmit()");
  await waitFor("window.SmartLibUI.state.settings.loan_days===21 && document.querySelector('#policy-form [name=loan_days]')?.value==='21'", 'Policy saved');
  await screenshot('admin-settings');
  await route('profile');
  await fill('#profile-form',{name:'Alex Browser'});
  await evaluate("document.querySelector('#profile-form').requestSubmit()");
  await waitFor("document.querySelector('#sidebar-name').textContent==='Alex Browser'", 'Profile saved');
  await route('settings');
  const downloads = path.join(temporary,'downloads');
  await mkdir(downloads,{recursive:true});
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:downloads});
  await click('[data-action="backup-download"]');
  let backupName;
  await until(async()=>{backupName=(await readdir(downloads)).find(name=>name.endsWith('.sqlite3'));return Boolean(backupName);},'Backup downloaded');
  const backupPath = path.join(downloads,backupName);
  assert((await readFile(backupPath)).subarray(0,16).toString()==='SQLite format 3\0','SQLite snapshot downloaded');
  await click('[data-action="backup-restore"]');
  await command('DOM.enable');
  const documentNode = await command('DOM.getDocument');
  const fileInput = await command('DOM.querySelector',{nodeId:documentNode.root.nodeId,selector:'#modal-body input[name=file]'});
  await command('DOM.setFileInputFiles',{nodeId:fileInput.nodeId,files:[backupPath]});
  await fill('#modal-body form',{password:'Admin@12345',confirmation:'RESTORE'});
  await screenshot('restore-confirmation');
  await submitModal();
  await waitFor("!document.querySelector('#auth-screen').hidden",'Restore signs out admin');
  await evaluate("document.querySelector('[data-demo=admin]').click();document.querySelector('#login-form').requestSubmit()");
  await waitFor("!document.querySelector('#app-shell').hidden",'Login after restore');
  await route('catalog');
  await command('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await sleep(150);
  assert(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Catalog fits mobile viewport');
  await screenshot('mobile-catalog');
  await evaluate("document.querySelector('#logout-button').click()");
  await waitFor("!document.querySelector('#auth-screen').hidden", 'Logout');
  await evaluate("document.querySelector('[data-demo=student]').click();document.querySelector('#login-form').requestSubmit()");
  await waitFor("!document.querySelector('#app-shell').hidden", 'Student sign-in');
  await evaluate("location.hash='#dashboard'");
  await sleep(400);
  await screenshot('student-dashboard-mobile');
  const nav = await evaluate("document.querySelector('#main-nav').innerText");
  assert(!/Members|Settings|Users|Backup/.test(nav), 'Student navigation hides admin tools');
  await route('catalog');
  const studentBookId = await evaluate("window.SmartLibUI.state.books.find(book=>book.title==='A Brief History of Time').id");
  await click(`[data-action="borrow"][data-id="${studentBookId}"]`);
  await screenshot('student-borrow-mobile');
  await submitModal();
  await route('loans');
  const studentLoanId = await evaluate(`window.SmartLibUI.state.loans.find(loan=>loan.book_id===${studentBookId} && !loan.returned_at).id`);
  await click(`[data-action="return"][data-id="${studentLoanId}"]`);
  await submitModal();
  assert(await evaluate(`window.SmartLibUI.state.loans.find(loan=>loan.id===${studentLoanId}).status==='returned'`),'Student returns own loan');
  assert(errors.length === 0, `Browser errors: ${errors.join('\n')}`);
  console.log('Browser smoke passed: both roles, record forms, lending/returns/fines, policy/profile, backup/restore, mobile layout, no JS errors.');
  console.log(`Screenshots: ${output}`);
} finally {
  if (socket?.readyState === WebSocket.OPEN) {
    try { await command('Browser.close'); } catch { /* Chrome closes its socket first. */ }
    socket.close();
  }
  if (chrome && chrome.exitCode === null) chrome.kill();
  if (server && server.exitCode === null) server.kill();
  await sleep(300);
  // Only remove the uniquely generated browser test directory in the OS temp root.
  if (path.dirname(path.resolve(temporary)) === path.resolve(tmpdir()) && path.basename(temporary).startsWith('smartlib-browser-')) {
    await rm(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
  }
}

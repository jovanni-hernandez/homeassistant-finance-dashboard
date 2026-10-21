#!/usr/bin/env node
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

function argValue(name, def = null) {
  const idx = process.argv.indexOf(name);
  return idx >= 0 ? process.argv[idx + 1] : def;
}

const inputFile = argValue('--file', 'finance_dashboard/frontend/index.html');
const screenshot = argValue('--screenshot', '/data/v2/cache/opencode/finance_dashboard-validate.png');
const port = Number(argValue('--port', '9333'));
const timeoutMs = Number(argValue('--timeout-ms', '15000'));

const absInput = path.resolve(inputFile);
const url = absInput.startsWith('/') ? `file://${absInput}` : absInput;
const profileDir = `/data/v2/cache/opencode/chromium-validate-${Date.now()}`;

function spawnChromium() {
  const args = [
    '--headless',
    '--disable-gpu',
    '--no-sandbox',
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    'about:blank',
  ];
  return spawn('chromium', args, { stdio: ['ignore', 'pipe', 'pipe'] });
}

async function waitForVersion() {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (res.ok) return await res.json();
    } catch {}
    await sleep(200);
  }
  throw new Error('Timed out waiting for Chromium remote debugging');
}

function wsSend(ws, id, method, params = {}, sessionId) {
  ws.send(JSON.stringify({ id, method, params, sessionId }));
}

async function main() {
  const chrome = spawnChromium();
  let stderr = '';
  chrome.stderr.on('data', (d) => (stderr += d.toString()));

  try {
    const version = await waitForVersion();
    const ws = new WebSocket(version.webSocketDebuggerUrl);
    const pending = new Map();
    const events = [];
    let id = 0;
    let sessionId = null;

    const call = (method, params = {}, sid = sessionId) =>
      new Promise((resolve, reject) => {
        const msgId = ++id;
        pending.set(msgId, { resolve, reject });
        wsSend(ws, msgId, method, params, sid || undefined);
      });

    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id) {
        const p = pending.get(msg.id);
        if (!p) return;
        pending.delete(msg.id);
        if (msg.error) p.reject(new Error(JSON.stringify(msg.error)));
        else p.resolve(msg.result);
      } else if (msg.method) {
        if (msg.method === 'Runtime.exceptionThrown' || msg.method === 'Log.entryAdded' || msg.method === 'Runtime.consoleAPICalled') {
          events.push(msg);
        }
      }
    };

    await new Promise((resolve, reject) => {
      ws.onopen = resolve;
      ws.onerror = reject;
    });

    const target = await call('Target.createTarget', { url: 'about:blank' }, null);
    const attach = await call('Target.attachToTarget', { targetId: target.targetId, flatten: true }, null);
    sessionId = attach.sessionId;

    await call('Runtime.enable');
    await call('Log.enable');
    await call('Page.enable');
    await call('Page.navigate', { url });
    await sleep(6500);

    const title = await call('Runtime.evaluate', { expression: 'document.title', returnByValue: true });
    const body = await call('Runtime.evaluate', { expression: 'document.body.innerText', returnByValue: true });
    const shot = await call('Page.captureScreenshot', { format: 'png', fromSurface: true });
    await fs.writeFile(screenshot, Buffer.from(shot.data, 'base64'));

    const exceptions = events.filter((e) => e.method === 'Runtime.exceptionThrown');
    const bodyText = body.result?.value || '';
    const result = {
      ok: exceptions.length === 0 && bodyText.length > 50,
      title: title.result?.value || '',
      bodyLength: bodyText.length,
      screenshot,
      exceptions: exceptions.length,
      consoleEvents: events.length,
    };

    console.log(JSON.stringify(result, null, 2));
    if (!result.ok) {
      process.exitCode = 1;
    }
    ws.close();
  } catch (err) {
    console.error(String(err));
    if (stderr) console.error(stderr.slice(-2000));
    process.exitCode = 1;
  } finally {
    chrome.kill('SIGTERM');
  }
}

main();

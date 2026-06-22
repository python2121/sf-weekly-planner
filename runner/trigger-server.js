const http = require('http');
const { spawn } = require('child_process');

const PORT = 5001;
const RUN_TIME = process.env.RUN_TIME || '10:30';

let running = false;

const ts = () => `[${new Date().toISOString()}]`;

function runDaily(force) {
  if (running) {
    console.log(`${ts()} run requested but already running`);
    return Promise.resolve({ status: 'busy' });
  }
  running = true;
  const promptArg = force ? '/sf-daily --force' : '/sf-daily';
  console.log(`${ts()} spawn: claude -p "${promptArg}"`);
  return new Promise((resolve) => {
    const proc = spawn(
      'claude',
      ['-p', promptArg, '--dangerously-skip-permissions'],
      { cwd: '/work', stdio: 'inherit' }
    );
    proc.on('exit', (code) => {
      console.log(`${ts()} claude exited code=${code}`);
      running = false;
      resolve({ status: 'done', code });
    });
    proc.on('error', (err) => {
      console.error(`${ts()} spawn error:`, err);
      running = false;
      resolve({ status: 'error' });
    });
  });
}

function msUntilNext(hhmm) {
  const [h, m] = hhmm.split(':').map(Number);
  const now = new Date();
  const target = new Date(now);
  target.setHours(h, m, 0, 0);
  if (target <= now) target.setDate(target.getDate() + 1);
  return target - now;
}

function scheduleNext() {
  const ms = msUntilNext(RUN_TIME);
  const at = new Date(Date.now() + ms);
  console.log(`${ts()} next scheduled run at ${at.toISOString()} (in ${Math.round(ms / 1000)}s)`);
  setTimeout(async () => {
    await runDaily(false);
    scheduleNext();
  }, ms);
}

const server = http.createServer((req, res) => {
  if (req.method === 'POST' && req.url === '/run') {
    if (running) {
      res.writeHead(409, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({ status: 'busy' }));
    }
    res.writeHead(202, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'started' }));
    runDaily(true);
    return;
  }
  if (req.method === 'GET' && req.url === '/status') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ running }));
  }
  res.writeHead(404);
  res.end();
});

server.listen(PORT, () => {
  console.log(`${ts()} trigger server on :${PORT}, RUN_TIME=${RUN_TIME}`);
  runDaily(false).then(scheduleNext);
});

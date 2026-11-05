// A scripted model with an OpenAI-compatible /v1/chat/completions (streamed), for the live test: no model
// key. What it answers depends on the marker in the last user message and on how many tool results came
// after it. Every request and every tool result is logged as JSON lines to $MOCK_LOG.
//
//   PROJECT_DIR=/path/to/project MOCK_PORT=4011 MOCK_LOG=/tmp/mock.jsonl node mock-model.mjs
import { appendFileSync } from 'node:fs';
import http from 'node:http';

const port = Number(process.env.MOCK_PORT ?? 4011);
const project = process.env.PROJECT_DIR ?? '/home/prized/e2e/app';
const logFile = process.env.MOCK_LOG ?? '/tmp/mock.jsonl';
const log = (o) => appendFileSync(logFile, `${JSON.stringify({ t: Date.now(), ...o })}\n`);

const bash = (command, description) => ({ name: 'bash', args: { command, description } });

/** Each turn: the tool calls in order, then a closing text. */
const SCRIPTS = {
  'E2E-TURN-1': [
    bash('pwd && ls -la && cat app.py && env | grep -c PRIZED_ || true', 'look around'),
    bash('nohup python3 -m http.server 8765 >/tmp/srv.log 2>&1 & echo $! > /home/prized/srv.pid; sleep 1; echo started $(cat /home/prized/srv.pid)', 'start a server'),
    { name: 'edit', args: { filePath: `${project}/app.py`, oldString: 'print("Hi")', newString: 'print("Hello from the sandbox")' } },
    { name: 'write', args: { filePath: `${project}/notes/sandbox.md`, content: 'written in the sandbox\n' } },
    bash("printf '#!/bin/sh\\necho deploy\\n' > deploy.sh && chmod +x deploy.sh && echo 'echo extra' >> bin/run", 'add an executable and edit one'),
    { name: 'read', args: { filePath: `${project}/app.py` } },
    { name: 'glob', args: { pattern: '**/*.py' } },
    { name: 'grep', args: { pattern: 'Hello', include: '*.py' } },
    { name: 'read', args: { filePath: `${project}/.env` } },
    'Turn one done.',
  ],
  'E2E-TURN-2': [
    bash("kill -0 $(cat /home/prized/srv.pid) && echo SERVER-ALIVE pid=$(cat /home/prized/srv.pid) && curl -s -o /dev/null -w 'http=%{http_code}\\n' http://127.0.0.1:8765/", 'is the server still up'),
    'Turn two done.',
  ],
  'E2E-IDLE': [bash('echo idle-turn && date +%s > /home/prized/idle.txt', 'a short turn'), 'Idle turn done.'],
  'E2E-AFTER-PAUSE': [bash('kill -0 $(cat /home/prized/srv.pid) && echo ALIVE-AFTER-PAUSE', 'still up after the pause'), 'After pause done.'],
  'E2E-FORK': [bash('kill -0 $(cat /home/prized/srv.pid) && echo FORK-SERVER-ALIVE; echo fork-only > fork.txt; ls', 'in the fork'), 'Fork done.'],
  'E2E-TURN-3': [bash('test -f fork.txt && echo PARENT-HAS-FORK-FILE || echo PARENT-CLEAN', 'parent untouched by the fork'), 'Turn three done.'],
};

function textOf(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.map((c) => (typeof c === 'string' ? c : (c.text ?? ''))).join('\n');
  return '';
}

function decide(body) {
  const msgs = body.messages ?? [];
  if (!body.tools || body.tools.length === 0) return { text: 'E2E session' }; // the title model
  let lastUser = -1;
  for (let i = msgs.length - 1; i >= 0; i -= 1) {
    if (msgs[i].role === 'user') {
      lastUser = i;
      break;
    }
  }
  const user = lastUser >= 0 ? textOf(msgs[lastUser].content) : '';
  const after = msgs.slice(lastUser + 1);
  for (const m of after) if (m.role === 'tool') log({ kind: 'tool_result', marker: Object.keys(SCRIPTS).find((k) => user.includes(k)) ?? null, id: m.tool_call_id, content: textOf(m.content) });
  const step = after.filter((m) => m.role === 'tool').length;
  const marker = Object.keys(SCRIPTS).find((k) => user.includes(k));
  if (!marker) return { text: user.includes('The user ran /') ? 'Done.' : 'OK.' };
  const item = SCRIPTS[marker][step];
  if (item === undefined || typeof item === 'string') return { text: item ?? 'Done.' };
  return { tool: { id: `call_${marker}_${step}`, name: item.name, args: item.args } };
}

const server = http.createServer((req, res) => {
  let raw = '';
  req.on('data', (d) => (raw += d));
  req.on('end', () => {
    if (!req.url?.endsWith('/chat/completions')) {
      res.writeHead(404).end();
      return;
    }
    const body = JSON.parse(raw || '{}');
    const answer = decide(body);
    const users = (body.messages ?? []).filter((m) => m.role === 'user');
    const user = users.length ? textOf(users[users.length - 1].content).slice(0, 8000) : '';
    log({ kind: 'request', tools: (body.tools ?? []).map((t) => ({ name: t.function?.name, parameters: t.function?.parameters })), user, answer });
    const id = `chatcmpl-${Date.now()}`;
    const chunk = (delta, finish = null) => `data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model: body.model, choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`;
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' });
    res.write(chunk({ role: 'assistant', content: '' }));
    if (answer.tool) {
      res.write(chunk({ tool_calls: [{ index: 0, id: answer.tool.id, type: 'function', function: { name: answer.tool.name, arguments: JSON.stringify(answer.tool.args) } }] }));
      res.write(chunk({}, 'tool_calls'));
    } else {
      res.write(chunk({ content: answer.text }));
      res.write(chunk({}, 'stop'));
    }
    res.write(`data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model: body.model, choices: [], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } })}\n\n`);
    res.end('data: [DONE]\n\n');
  });
});
server.listen(port, '127.0.0.1', () => log({ kind: 'listening', port }));

const http  = require('http');
const https = require('https');
const fs    = require('fs');
const path  = require('path');

const PORT     = 3939;
const ROOT     = __dirname;
const ENV_PATH = path.join(ROOT, '.env');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.json': 'application/json',
  '.js':   'application/javascript',
  '.css':  'text/css',
};

// ── .env helpers ─────────────────────────────────────────────────
function readEnv() {
  var env = {};
  try {
    fs.readFileSync(ENV_PATH, 'utf8').split('\n').forEach(function(line) {
      var m = line.match(/^([^=#\s][^=]*)=(.*)$/);
      if (m) env[m[1].trim()] = m[2].trim().replace(/^["']|["']$/g, '');
    });
  } catch(e) {}
  return env;
}

function writeEnv(updates) {
  var env = readEnv();
  Object.assign(env, updates);
  var content = Object.keys(env).map(function(k) { return k + '=' + env[k]; }).join('\n') + '\n';
  fs.writeFileSync(ENV_PATH, content, 'utf8');
}

// ── Slack API helper ──────────────────────────────────────────────
function slackGet(token, method, params) {
  return new Promise(function(resolve, reject) {
    var qs = Object.keys(params).map(function(k) {
      return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]);
    }).join('&');
    var req = https.request({
      hostname: 'slack.com',
      path: '/api/' + method + (qs ? '?' + qs : ''),
      method: 'GET',
      headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }
    }, function(res) {
      var buf = '';
      res.on('data', function(d) { buf += d; });
      res.on('end', function() {
        try { resolve(JSON.parse(buf)); } catch(e) { reject(e); }
      });
    });
    req.on('error', reject);
    req.end();
  });
}

// チャンネル名 → ID（private含む全チャンネルを走査）
async function findChannelId(token, names) {
  var cursor = '';
  for (var i = 0; i < 10; i++) {
    var p = { limit: 1000, types: 'public_channel,private_channel', exclude_archived: true };
    if (cursor) p.cursor = cursor;
    var r = await slackGet(token, 'conversations.list', p);
    if (!r.ok) throw new Error('conversations.list: ' + r.error);
    var found = r.channels.find(function(c) {
      return names.some(function(n) { return c.name === n || c.name_normalized === n; });
    });
    if (found) return found.id;
    cursor = r.response_metadata && r.response_metadata.next_cursor;
    if (!cursor) break;
  }
  return null;
}

// チャンネルの全メッセージを取得してフィルタ
async function fetchEventMessages(token) {
  var candidateNames = ['26卒メンバー', '26卒-メンバー', '26卒_メンバー'];
  var channelId = await findChannelId(token, candidateNames);
  if (!channelId) {
    throw new Error('チャンネル「26卒メンバー」が見つかりません。Slackアプリをチャンネルに招待してください。');
  }

  var messages = [];
  var cursor   = '';
  for (var page = 0; page < 8; page++) {
    var p = { channel: channelId, limit: 200 };
    if (cursor) p.cursor = cursor;
    var r = await slackGet(token, 'conversations.history', p);
    if (!r.ok) throw new Error('conversations.history: ' + r.error);
    messages = messages.concat(r.messages || []);
    cursor = r.response_metadata && r.response_metadata.next_cursor;
    if (!cursor) break;
  }

  // #社内イベント を含むメッセージのみ
  var filtered = messages.filter(function(m) {
    return m.text && m.text.includes('#社内イベント') && m.type === 'message';
  });

  return filtered.map(function(m) {
    var ts   = parseFloat(m.ts || 0);
    var date = ts ? new Date(ts * 1000).toLocaleDateString('ja-JP') : '';
    var text = m.text.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
    // 最初の50文字をタイトル候補に
    var preview = text.length > 80 ? text.slice(0, 80) + '…' : text;
    return { ts: m.ts, date: date, text: text, preview: preview };
  });
}

// ── JSON response helper ──────────────────────────────────────────
function jsonRes(res, status, data) {
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type'
  });
  res.end(JSON.stringify(data));
}

function bodyJson(req) {
  return new Promise(function(resolve, reject) {
    var buf = '';
    req.on('data', function(d) { buf += d; });
    req.on('end', function() {
      try { resolve(JSON.parse(buf || '{}')); } catch(e) { reject(e); }
    });
  });
}

// ── HTTP server ───────────────────────────────────────────────────
http.createServer(function(req, res) {
  var urlPath = req.url.split('?')[0];

  // CORS preflight
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type'
    });
    res.end();
    return;
  }

  // ── API routes ──────────────────────────────────────────────────
  if (urlPath === '/api/credentials-status') {
    var env = readEnv();
    jsonRes(res, 200, {
      slack_token_set: !!(env.SLACK_BOT_TOKEN && env.SLACK_BOT_TOKEN.startsWith('xox')),
      gemini_key_set:  !!(env.GEMINI_API_KEY)
    });
    return;
  }

  if (urlPath === '/api/set-credentials' && req.method === 'POST') {
    bodyJson(req).then(function(body) {
      var updates = {};
      if (body.slack_token) updates.SLACK_BOT_TOKEN = body.slack_token;
      if (body.gemini_key)  updates.GEMINI_API_KEY  = body.gemini_key;
      writeEnv(updates);
      jsonRes(res, 200, { ok: true });
    }).catch(function(e) {
      jsonRes(res, 400, { error: e.message });
    });
    return;
  }

  if (urlPath === '/api/slack-events') {
    var env = readEnv();
    var token = env.SLACK_BOT_TOKEN;
    if (!token) {
      jsonRes(res, 401, { error: 'SLACK_BOT_TOKEN が設定されていません。⚙️ から設定してください。' });
      return;
    }
    fetchEventMessages(token).then(function(events) {
      jsonRes(res, 200, { ok: true, events: events });
    }).catch(function(e) {
      jsonRes(res, 500, { error: e.message });
    });
    return;
  }

  // ── Static files ────────────────────────────────────────────────
  var filePath = path.join(ROOT, urlPath === '/' ? '/prompt_generator.html' : urlPath);
  // 簡易ディレクトリトラバーサル防止
  if (!filePath.startsWith(ROOT)) { res.writeHead(403); res.end('Forbidden'); return; }
  fs.readFile(filePath, function(err, data) {
    if (err) { res.writeHead(404); res.end('Not found'); return; }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(filePath)] || 'text/plain' });
    res.end(data);
  });
}).listen(PORT, '127.0.0.1', function() {
  console.log('Server: http://localhost:' + PORT);
});

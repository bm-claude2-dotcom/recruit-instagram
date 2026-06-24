const http = require('http');
const fs   = require('fs');
const path = require('path');

const PORT = 3939;
const ROOT = __dirname;
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.json': 'application/json',
  '.js':   'application/javascript',
  '.css':  'text/css',
};

http.createServer(function(req, res) {
  var urlPath  = req.url === '/' ? '/prompt_generator.html' : req.url.split('?')[0];
  var filePath = path.join(ROOT, urlPath);
  fs.readFile(filePath, function(err, data) {
    if (err) { res.writeHead(404); res.end('Not found'); return; }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(filePath)] || 'text/plain' });
    res.end(data);
  });
}).listen(PORT, '127.0.0.1', function() {
  console.log('Server running at http://localhost:' + PORT);
});

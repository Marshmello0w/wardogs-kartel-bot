// Rebuild the first-party copy from an already-built, pinned upstream checkout.
// Usage: node web/tools/vendor_artillery.mjs C:/path/to/wardogs-calculator/dist
import { cp, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { resolve, join, relative, sep } from 'node:path';

const source = resolve(process.argv[2] || '');
const target = resolve(import.meta.dirname, '..', 'artillery_app');
const webRoot = resolve(import.meta.dirname, '..');
if (!process.argv[2] || !relative(webRoot, target) || relative(webRoot, target).startsWith('..' + sep)) {
  throw new Error('Invalid artillery destination');
}
await readFile(join(source, 'LICENSE'));
await readFile(join(source, 'index.html'));
await mkdir(target, { recursive: true });
await cp(source, target, { recursive: true, force: true, filter: path => !/[\\/](?:CNAME|robots\.txt|sitemap\.xml)$/.test(path) });

async function files(dir) {
  const result = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) result.push(...await files(path));
    else result.push(path);
  }
  return result;
}

const localCsp = "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; worker-src 'self' blob:; frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'";
for (const path of await files(target)) {
  if (path.endsWith('.html')) {
    let html = await readFile(path, 'utf8');
    html = html.replace(/<meta[^>]*http-equiv="Content-Security-Policy"[^>]*\/>/g, '');
    html = html.replace(/<link[^>]*assets\.wardogs-artillery\.com[^>]*\/>/g, '');
    html = html.replace(/<script[^>]*src="https:\/\/cloud\.umami\.is\/script\.js"[^>]*><\/script>/g, '');
    html = html.replace(/<meta[^>]*name="robots"[^>]*\/>/g, '<meta name="robots" content="noindex, nofollow"/>');
    html = html.replace('<head>', '<head>\n<script src="/artillery-app/privacy.js"></script>\n<meta http-equiv="Content-Security-Policy" content="' + localCsp + '">');
    html = html.replace('</head>', '<link rel="stylesheet" href="/artillery-app/kartell-theme.css">\n</head>');
    await writeFile(path, html);
  }
}

const appConfigPath = join(target, 'config', 'app.json');
const config = JSON.parse(await readFile(appConfigPath, 'utf8'));
config.collab.enabled = false;
config.collab.serverUrl = '';
config.collab.turnstile.enabled = false;
config.feedback.enabled = false;
config.feedback.serverUrl = '';
config.site.footer.authorName = 'Apollyon · wardogs-artillery.com';
config.site.footer.authorUrl = 'https://wardogs-artillery.com/';
await writeFile(appConfigPath, JSON.stringify(config, null, 2) + '\n');

const remote = 'https://assets.wardogs-artillery.com/releases/assets-v1/';
for (const path of [
  join(target, 'maps', 'bakurani.json'),
  join(target, 'maps', 'ozeti.json'),
  join(target, 'maps', 'zestafona.json'),
  join(target, 'data', 'ballistics', 'terrain-context.json'),
]) {
  const content = await readFile(path, 'utf8');
  await writeFile(path, content.replaceAll(remote, '/artillery-assets/'));
}
await writeFile(join(target, 'privacy.js'), 'window.__WARDOGS_ANALYTICS_DISABLED__ = true;\n');
console.log(`Vendored WARDOGS calculator into ${target}`);

"""Build a browser-local dashboard. No server, third-party assets or network."""
from pathlib import Path
import secrets

root = Path(__file__).parent
ui = root / 'ui'
html = (ui / 'index.html').read_text()
nonce = secrets.token_urlsafe(24)
policy = "default-src 'none'; script-src 'nonce-%s'; style-src 'unsafe-inline'; connect-src 'none'; base-uri 'none'; form-action 'none'" % nonce
html = html.replace('<meta charset="utf-8">', '<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'+policy+'">')
html = html.replace('<link rel="stylesheet" href="/style.css">', '<style>'+ (ui / 'style.css').read_text()+'</style>')
html = html.replace('<script src="/app.js" defer></script>', '')
html = html.replace('href="/"', 'href="#overview"')
scripts = '\n'.join((ui / name).read_text() for name in ('offline.js','app.js'))
html = html.replace('</body>', '<script nonce="'+nonce+'">'+scripts+'</script></body>')
(root / 'Codex Relay.html').write_text(html)
print('Built Codex Relay.html (local browser only, network disabled).')

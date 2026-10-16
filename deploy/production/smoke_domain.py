"""Production smoke checks. Run as root on the VPS; prints no credentials/tokens."""
import http.cookiejar
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

ORIGIN = 'https://quantik.io.vn'
cookies = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
results = []


def request(path, method='GET', body=None, token=None, origin=ORIGIN):
    headers = {'Origin': origin}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(ORIGIN + path, headers=headers, method=method,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        response = opener.open(req, timeout=25)
    except urllib.error.HTTPError as exc:
        response = exc
    raw = response.read()
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        payload = None
    return response.status, response.headers, raw, payload


def check(name, actual, expected):
    passed = actual == expected
    results.append({'check': name, 'passed': passed, 'actual': actual, 'expected': expected})
    print(('PASS ' if passed else 'FAIL ') + name + ': ' + str(actual), flush=True)


def main():
    status, headers, raw, _ = request('/')
    check('homepage', status, 200)
    for header in ('Strict-Transport-Security', 'X-Content-Type-Options', 'X-Frame-Options'):
        check('header ' + header, bool(headers.get(header)), True)
    check('no X-Powered-By', headers.get('X-Powered-By') is None, True)
    script = re.search(rb'src="(/_next/static/[^"?]+\.js)', raw)
    check('static JS reference', bool(script), True)
    if script:
        check('static JS response', request(script.group(1).decode())[0], 200)
    for path, code in (('/health/ready',200), ('/health/live',200), ('/api/v1/plans',200),
        ('/api/v1/admin/me',401), ('/.env',404), ('/.git/config',404), ('/docs',404),
        ('/openapi.json',404), ('/internal/metrics',404)):
        check(path, request(path)[0], code)
    html = request('/admin')[2].decode().replace('\\"', '"')
    check('anonymous admin content gated', '"verified":false' in html and '"children":null' in html, True)
    admin = dict(line.split('=',1) for line in Path('/root/quantik-initial-admin.txt').read_text().splitlines())
    status = request('/api/v1/admin/auth/login', 'POST', admin, origin='https://invalid.example')[0]
    check('cross-origin login rejected', status, 403)
    status, headers, _, data = request('/api/v1/admin/auth/login', 'POST', admin)
    check('admin login', status, 200)
    if status != 200:
        raise SystemExit('Admin login failed; response suppressed')
    for name in ('quantik_admin_refresh', 'quantik_admin_gate'):
        matched = [c for c in headers.get_all('Set-Cookie', []) if c.startswith(name + '=')]
        check(name + ' cookie protections', bool(matched) and all(s in matched[0].lower() for s in ('httponly','secure','samesite=strict')), True)
    token = data['access_token']
    check('admin authenticated profile', request('/api/v1/admin/me', token=token)[0], 200)
    html = request('/admin')[2].decode().replace('\\"', '"')
    check('admin SSR page authenticated', '"verified":true' in html, True)
    check('admin users', request('/api/v1/admin/users', token=token)[0], 200)
    health = request('/api/v1/admin/operations/health', token=token)
    check('admin operations health', health[0], 200)
    if health[0] == 200:
        Path('/opt/quantik/operations-health.json').write_text(json.dumps(health[3], indent=2))
    check('cross-origin refresh rejected', request('/api/v1/admin/auth/refresh', 'POST', {}, origin='https://invalid.example')[0], 403)
    status, _, _, data = request('/api/v1/admin/auth/refresh', 'POST', {})
    check('refresh rotation', status, 200)
    if status == 200:
        token = data['access_token']
    check('logout', request('/api/v1/admin/auth/logout', 'POST', {})[0], 204)
    check('access revoked after logout', request('/api/v1/admin/me', token=token)[0], 401)
    status, _, _, data = request('/api/v1/auth/google/start')
    if status == 200:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(data['authorization_url']).query)
        check('Google redirect URI', query.get('redirect_uri', [''])[0], ORIGIN+'/api/v1/auth/google/callback')
    else:
        results.append({'check':'Google OAuth start', 'status':status, 'live_consent_verified':False})
    out = Path('/opt/quantik/smoke-results.json')
    out.write_text(json.dumps(results, indent=2))
    out.chmod(0o600)
    if any(x.get('passed') is False for x in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()

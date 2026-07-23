from flask import Flask, request, jsonify, send_from_directory
import requests
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', 'https://ptrain.onrender.com')
SUPABASE_URL = 'https://ysztaecwyjuqchmcgxjr.supabase.co'
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', 'sb_publishable_2Y6Azzpv7SNDVRMng3QU7Q_bhiWuV2i')
EO_API_KEY = os.environ.get('EO_API_KEY', '')
EO_LIST_ID = 'badf6dd4-6fa7-11f1-bb46-e1bea4e2db03'
RAPIDAPI_KEY = os.environ.get('RAPIDAPI_KEY', '')
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')

SUPABASE_HEADERS = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json',
    'Prefer': 'return=representation'
}

# ─── SECURITY: CORS, ORIGIN CHECK, RATE LIMITING ─────────────────────────────

ALLOWED_ORIGINS = {
    'https://ptrain.app',
    'https://www.ptrain.app',
    RENDER_URL.rstrip('/'),
}

def cors(response):
    origin = request.headers.get('Origin', '')
    allow = origin if origin in ALLOWED_ORIGINS else 'https://ptrain.app'
    response.headers['Access-Control-Allow-Origin'] = allow
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Vary'] = 'Origin'
    return response

def origin_ok():
    """Block only requests that clearly come from a foreign website.
    Missing Origin headers and same-host requests are always allowed so real
    users (PWA standalone mode, in-app browsers) can never be locked out."""
    origin = request.headers.get('Origin', '')
    if not origin:
        return True
    if origin in ALLOWED_ORIGINS:
        return True
    try:
        from urllib.parse import urlparse
        return urlparse(origin).netloc == request.host
    except Exception:
        return True

# Simple in-memory per-IP rate limiter (resets on redeploy — fine for now)
RATE_LIMIT = 30        # max AI calls per IP
RATE_WINDOW = 3600     # per hour
_hits = defaultdict(deque)
_hits_lock = threading.Lock()

# Server-side plan quotas — must match the limits shown in index.html's PLANS
# object exactly, since that's the promise being made to the user. This is
# enforced here (not just in the frontend) so nobody can bypass their plan
# limit by calling /api/generate directly.
PLAN_LIMITS = {'free': 1, 'pro': 10, 'coach': 50, 'elite': None}  # None = unlimited

def get_user_plan_state(email):
    """Look up a user's current plan tier and plans-used count from Supabase.
    Also handles the monthly quota reset: if 30+ days have passed since their
    current billing period started, their usage count resets to 0 here (and
    that reset is saved back immediately) rather than accumulating forever.
    Returns ('free', 0) for unknown/anonymous users — the safe default."""
    try:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_users?email=eq.{email}&select=state',
            headers=SUPABASE_HEADERS, timeout=10
        )
        rows = r.json()
        if not rows or len(rows) == 0:
            return 'free', 0
        state = rows[0].get('state') or {}
        plan = state.get('userPlan', 'free')
        used = state.get('plansUsed', 0)
        period_start_str = state.get('planPeriodStart')
        needs_reset = False
        if plan != 'free':  # free tier's limit is a lifetime cap, not monthly — never auto-reset it
            needs_reset = True
            if period_start_str:
                try:
                    period_start = datetime.fromisoformat(period_start_str)
                    needs_reset = (datetime.now(timezone.utc) - period_start) >= timedelta(days=30)
                except Exception:
                    needs_reset = True  # unparsable date — treat as needing a fresh period
        if needs_reset:
            used = 0
            state['plansUsed'] = 0
            state['planPeriodStart'] = datetime.now(timezone.utc).isoformat()
            try:
                requests.post(
                    f'{SUPABASE_URL}/rest/v1/ptrain_users',
                    headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
                    json={'email': email, 'state': state, 'updated_at': 'now()'},
                    timeout=10
                )
            except Exception as e:
                print(f'[get_user_plan_state] period reset write failed for {email}: {e}')
        return plan, used
    except Exception as e:
        print(f'[get_user_plan_state] lookup failed for {email}: {e}')
    return 'free', 0

def increment_plans_used(email, current_plan, current_used):
    """Bump plansUsed by 1 after a successful generation, preserving the rest
    of the user's existing state (same merge-not-overwrite pattern as upgrade_plan)."""
    try:
        lookup = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_users?email=eq.{email}&select=state',
            headers=SUPABASE_HEADERS, timeout=10
        )
        rows = lookup.json()
        existing_state = (rows[0].get('state') or {}) if rows else {}
        existing_state['userPlan'] = current_plan
        existing_state['plansUsed'] = current_used + 1
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/ptrain_users',
            headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
            json={'email': email, 'state': existing_state, 'updated_at': 'now()'},
            timeout=10
        )
        if r.status_code >= 300:
            # This is the write that enforces plan limits (e.g. free tier's "1 plan
            # ever"). If it silently failed before, the count never actually moved
            # in Supabase and the limit was effectively unenforceable.
            print(f'[increment_plans_used] Supabase rejected the write for {email}: {r.status_code} {r.text[:200]}')
    except Exception as e:
        print(f'[increment_plans_used] failed for {email}: {e}')

def client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.remote_addr or 'unknown'

def rate_limited(ip):
    now = time.time()
    with _hits_lock:
        q = _hits[ip]
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return True
        q.append(now)
    return False

# ─── STATIC / SEO ROUTES ──────────────────────────────────────────────────────

def send_html(name):
    """Serve an HTML page with caching disabled so phones always get the
    latest deployed version — prevents installed PWAs showing stale copies."""
    resp = send_from_directory(BASE_DIR, name)
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@app.route('/')
def home():
    return send_html('index.html')

@app.route('/business')
def business():
    return send_html('business.html')

@app.route('/privacy')
def privacy():
    return send_html('privacy.html')

@app.route('/terms')
def terms():
    return send_html('terms.html')

@app.route('/embed.js')
def embed_js():
    js = """(function(){
var d=document,s=d.currentScript,ref=(s&&s.getAttribute('data-ref'))||'embed';
var w=d.createElement('div');
w.style.cssText='max-width:420px;background:#0a0a0d;border:1px solid rgba(255,90,31,.35);border-radius:18px;padding:22px;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#fff';
w.innerHTML='<div style=\\"font-weight:900;font-size:19px;letter-spacing:-.02em;margin-bottom:4px\\">PTr<span style=\\"color:#ff5a1f\\">AI</span>n</div>'+
'<div style=\\"font-weight:800;font-size:16px;color:#fff;margin-bottom:6px\\">A personal trainer in your pocket.</div>'+
'<div style=\\"font-size:13px;color:#c8c8d0;line-height:1.5;margin-bottom:14px\\">Full training plan + nutrition plan built around you in 60 seconds. First week free, no card.</div>'+
'<a href=\\"https://ptrain.app?ref='+ref+'\\" style=\\"display:block;text-align:center;background:#ff5a1f;color:#fff;font-weight:800;font-size:14px;padding:12px;border-radius:11px;text-decoration:none\\">Build my free plan →</a>';
s.parentNode.insertBefore(w,s);
})();"""
    return js, 200, {'Content-Type': 'application/javascript', 'Access-Control-Allow-Origin': '*'}

@app.route('/embed')
def embed_page():
    html = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Embed PTrAIn on your site</title><style>body{background:#0a0a0d;color:#fff;font-family:-apple-system,sans-serif;max-width:640px;margin:0 auto;padding:48px 24px;line-height:1.6}h1{font-weight:900;letter-spacing:-.03em}h1 span{color:#ff5a1f}code{display:block;background:#16161c;border:1px solid rgba(245,245,240,.1);border-radius:12px;padding:16px;font-size:13px;color:#ff8a4f;word-break:break-all;margin:16px 0}p{color:#c8c8d0}</style></head><body><h1>Put PTrAIn <span>on your site.</span></h1><p>PTs, gyms, bloggers — paste this one line anywhere in your page. A PTrAIn card appears, your visitors get a free week's plan, and if you're an affiliate your ref is tracked automatically.</p><code>&lt;script src="https://ptrain.app/embed.js" data-ref="YOURNAME"&gt;&lt;/script&gt;</code><p>Change <b>YOURNAME</b> to your affiliate handle. Questions: tel@ptrain.app</p></body></html>"""
    return html

@app.route('/free-workout-plan')
def free_workout_plan():
    return send_html('free-workout-plan.html')

@app.route('/ai-personal-trainer')
def ai_personal_trainer():
    return send_html('ai-personal-trainer.html')

@app.route('/compare')
def compare():
    return send_html('compare.html')

@app.route('/hyrox-training-plan')
def hyrox_training_plan():
    return send_html('hyrox-training-plan.html')

@app.route('/sitemap.xml')
def sitemap():
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://ptrain.app/</loc><priority>1.0</priority></url>
<url><loc>https://ptrain.app/business</loc><priority>0.9</priority></url>
<url><loc>https://ptrain.app/free-workout-plan</loc><priority>0.8</priority></url>
<url><loc>https://ptrain.app/ai-personal-trainer</loc><priority>0.8</priority></url>
<url><loc>https://ptrain.app/hyrox-training-plan</loc><priority>0.8</priority></url>
<url><loc>https://ptrain.app/compare</loc><priority>0.8</priority></url>
<url><loc>https://ptrain.app/privacy</loc><priority>0.3</priority></url>
</urlset>'''
    return xml, 200, {'Content-Type': 'application/xml'}

@app.route('/llms.txt')
def llms():
    txt = """# PTrAIn
> AI personal trainer app built by Tel Kershaw, a certified PT with 8 years at Gymbox London. Generates complete personalised training programmes and nutrition plans in 60 seconds.

- Consumer app: https://ptrain.app — first week's programme and nutrition plan free, Pro £6.99/month
- For companies: https://ptrain.app/business — employee wellbeing platform from £3.99/employee/month, GDPR compliant
- Covers: gym, home, bodyweight, CrossFit, Hyrox, calisthenics, sport-specific, yoga, endurance training
- Adaptive: tracks lifts and PBs, every new programme builds on real performance data
- Contact: tel@ptrain.app
"""
    return txt, 200, {'Content-Type': 'text/plain'}

@app.route('/robots.txt')
def robots():
    return 'User-agent: *\nAllow: /\nSitemap: https://ptrain.app/sitemap.xml', 200, {'Content-Type': 'text/plain'}

@app.route('/manifest.json')
def manifest():
    return send_from_directory(BASE_DIR, 'manifest.json')

@app.route('/icon-192.png')
def icon192():
    return send_from_directory(BASE_DIR, 'icon-192.png')

@app.route('/og-image.png')
def og_image():
    return send_from_directory(BASE_DIR, 'og-image.png')

@app.route('/icon-512.png')
def icon512():
    return send_from_directory(BASE_DIR, 'icon-512.png')

@app.route('/ping')
def ping():
    return jsonify({'status': 'ok'}), 200

def keep_alive():
    time.sleep(30)
    while True:
        try:
            requests.get(f'{RENDER_URL}/ping', timeout=10)
        except Exception:
            pass
        time.sleep(840)

# ─── STRIPE PAYMENT VERIFICATION ─────────────────────────────────────────────
# Stripe Payment Link redirect must be set (per link, in Stripe dashboard) to:
#   https://ptrain.app/?success=true&plan=pro&session_id={CHECKOUT_SESSION_ID}
# The frontend then calls this endpoint, which confirms with Stripe that the
# session was genuinely paid before the app unlocks the tier.

@app.route('/api/stripe/verify', methods=['POST', 'OPTIONS'])
def stripe_verify():
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    try:
        if not STRIPE_SECRET_KEY:
            return cors(jsonify({'ok': False, 'paid': False, 'error': 'STRIPE_SECRET_KEY not set'})), 500
        body = request.get_json() or {}
        session_id = str(body.get('session_id', '')).strip()
        if not session_id.startswith('cs_'):
            return cors(jsonify({'ok': False, 'paid': False, 'error': 'Invalid session id'})), 400
        r = requests.get(
            f'https://api.stripe.com/v1/checkout/sessions/{session_id}',
            auth=(STRIPE_SECRET_KEY, ''),
            timeout=15
        )
        data = r.json()
        # Stripe returned an error (wrong key, key/session mode mismatch, expired
        # session, etc.) — surface it instead of silently reporting paid:false.
        if r.status_code != 200:
            stripe_err = (data.get('error') or {}).get('message', f'Stripe returned HTTP {r.status_code}')
            print(f'[stripe_verify] Stripe API error for session {session_id}: {stripe_err}')
            return cors(jsonify({'ok': False, 'paid': False, 'error': stripe_err})), 502
        paid = data.get('payment_status') == 'paid'
        email = (data.get('customer_details') or {}).get('email', '')
        return cors(jsonify({'ok': True, 'paid': paid, 'email': email}))
    except Exception as e:
        return cors(jsonify({'ok': False, 'paid': False, 'error': str(e)})), 500

# ─── SUPABASE USER STATE ───────────────────────────────────────────────────────

@app.route('/api/state/load', methods=['POST', 'OPTIONS'])
def load_state():
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    try:
        email = request.get_json().get('email', '').lower().strip()
        if not email:
            return cors(jsonify({'error': 'No email'})), 400
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_users?email=eq.{email}&select=*',
            headers=SUPABASE_HEADERS, timeout=10
        )
        data = r.json()
        if data and len(data) > 0:
            return cors(jsonify({'found': True, 'state': data[0].get('state', {}), 'plan': data[0].get('plan')}))
        return cors(jsonify({'found': False}))
    except Exception as e:
        return cors(jsonify({'error': str(e)})), 500

@app.route('/api/referral/claim', methods=['POST', 'OPTIONS'])
def claim_referral():
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    try:
        body = request.get_json()
        new_email = (body.get('email') or '').lower().strip()
        ref_code = (body.get('ref') or '').lower().strip()
        if not new_email or not ref_code:
            return cors(jsonify({'error': 'Missing email or ref code'})), 400

        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_users?select=email,state',
            headers=SUPABASE_HEADERS, timeout=10
        )
        if r.status_code != 200:
            return cors(jsonify({'error': 'Lookup failed'})), 500
        users = r.json()
        referrer = None
        for u in users:
            if make_ref_code(u.get('email', '')) == ref_code:
                referrer = u
                break
        if not referrer:
            return cors(jsonify({'ok': True, 'matched': False}))
        if referrer['email'] == new_email:
            return cors(jsonify({'ok': True, 'matched': False}))

        new_user_r = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_users?email=eq.{new_email}&select=state',
            headers=SUPABASE_HEADERS, timeout=10
        )
        new_user_data = new_user_r.json()
        existing_state = new_user_data[0].get('state', {}) if new_user_data else {}
        if existing_state.get('referredBy'):
            return cors(jsonify({'ok': True, 'matched': False, 'reason': 'already_claimed'}))

        existing_state['referredBy'] = referrer['email']
        r1 = requests.post(
            f'{SUPABASE_URL}/rest/v1/ptrain_users',
            headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
            json={'email': new_email, 'state': existing_state, 'updated_at': 'now()'},
            timeout=10
        )
        if r1.status_code >= 300:
            print(f'[claim_referral] failed to save referredBy for {new_email}: {r1.status_code} {r1.text[:200]}')

        referrer_state = referrer.get('state', {}) or {}
        referrer_state['bonusPlans'] = referrer_state.get('bonusPlans', 0) + 1
        referred_list = referrer_state.get('referredUsers', [])
        if new_email not in referred_list:
            referred_list.append(new_email)
        referrer_state['referredUsers'] = referred_list
        r2 = requests.post(
            f'{SUPABASE_URL}/rest/v1/ptrain_users',
            headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
            json={'email': referrer['email'], 'state': referrer_state, 'updated_at': 'now()'},
            timeout=10
        )
        if r2.status_code >= 300:
            print(f'[claim_referral] failed to credit bonus plan to {referrer["email"]}: {r2.status_code} {r2.text[:200]}')
        return cors(jsonify({'ok': True, 'matched': True, 'referrer': referrer['email']}))
    except Exception as e:
        return cors(jsonify({'error': str(e)})), 500

def make_ref_code(email):
    """Deterministic, non-reversible-looking referral code from an email."""
    import hashlib
    return hashlib.sha256((email.lower().strip() + 'ptrain_ref_salt_v1').encode()).hexdigest()[:8]

@app.route('/api/referral/lookup', methods=['GET', 'OPTIONS'])
def lookup_referral_status():
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    try:
        email = (request.args.get('email') or '').lower().strip()
        if not email:
            return cors(jsonify({'error': 'No email'})), 400
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_users?email=eq.{email}&select=state',
            headers=SUPABASE_HEADERS, timeout=10
        )
        data = r.json()
        state = data[0].get('state', {}) if data else {}
        return cors(jsonify({
            'ok': True,
            'refCode': make_ref_code(email),
            'bonusPlans': state.get('bonusPlans', 0),
            'referredCount': len(state.get('referredUsers', []))
        }))
    except Exception as e:
        return cors(jsonify({'error': str(e)})), 500

@app.route('/api/state/save', methods=['POST', 'OPTIONS'])
def save_state():
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    try:
        body = request.get_json()
        email = body.get('email', '').lower().strip()
        if not email:
            return cors(jsonify({'error': 'No email'})), 400
        state = body.get('state', {})
        plan = body.get('plan')
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/ptrain_users',
            headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
            json={'email': email, 'state': state, 'plan': plan, 'updated_at': 'now()'},
            timeout=10
        )
        return cors(jsonify({'ok': True, 'status': r.status_code}))
    except Exception as e:
        return cors(jsonify({'error': str(e)})), 500

@app.route('/api/state/upgrade', methods=['POST', 'OPTIONS'])
def upgrade_plan():
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    try:
        body = request.get_json()
        email = body.get('email', '').lower().strip()
        plan_tier = body.get('plan', 'pro')
        if not email:
            return cors(jsonify({'error': 'No email'})), 400
        # Fetch the user's existing state first — writing a bare {userPlan,
        # plansUsed} dict here would silently wipe their history, PBs, and
        # referral data, since Supabase upsert replaces the whole state
        # column rather than deep-merging the JSON.
        existing_state = {}
        try:
            lookup = requests.get(
                f'{SUPABASE_URL}/rest/v1/ptrain_users?email=eq.{email}&select=state',
                headers=SUPABASE_HEADERS, timeout=10
            )
            existing_data = lookup.json()
            if existing_data and len(existing_data) > 0:
                existing_state = existing_data[0].get('state') or {}
        except Exception as lookup_err:
            print(f'[upgrade_plan] state lookup failed for {email}: {lookup_err}')
        existing_state['userPlan'] = plan_tier
        existing_state['plansUsed'] = 0
        existing_state['planPeriodStart'] = datetime.now(timezone.utc).isoformat()
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/ptrain_users',
            headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
            json={'email': email, 'state': existing_state, 'updated_at': 'now()'},
            timeout=10
        )
        if r.status_code >= 300:
            print(f'[upgrade_plan] Supabase write failed for {email}: {r.status_code} {r.text}')
            return cors(jsonify({'ok': False, 'error': f'Supabase write failed: {r.status_code}'})), 502
        return cors(jsonify({'ok': True}))
    except Exception as e:
        return cors(jsonify({'error': str(e)})), 500

# ─── REVIEWS ──────────────────────────────────────────────────────────────────

@app.route('/api/review', methods=['POST', 'OPTIONS'])
def submit_review():
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    try:
        body = request.get_json()
        stars = int(body.get('stars', 0))
        if stars < 1 or stars > 5:
            return cors(jsonify({'error': 'Invalid rating'})), 400
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/ptrain_reviews',
            headers={**SUPABASE_HEADERS, 'Prefer': 'return=minimal'},
            json={
                'stars': stars,
                'name': str(body.get('name', ''))[:60],
                'text': str(body.get('text', ''))[:500],
                'email': str(body.get('email', '')).lower().strip()[:120]
            },
            timeout=10
        )
        if r.status_code >= 300:
            # This previously reported ok:true regardless of what Supabase actually
            # did — meaning a rejected write looked identical to a saved one. Now it
            # surfaces the real reason so a repeat of this is diagnosable, not guessed at.
            print(f'[submit_review] Supabase rejected the write: {r.status_code} {r.text}')
            return cors(jsonify({'ok': False, 'error': f'Save failed ({r.status_code})'})), 502
        return cors(jsonify({'ok': True, 'status': r.status_code}))
    except Exception as e:
        print(f'[submit_review] exception: {e}')
        return cors(jsonify({'ok': False, 'error': str(e)})), 500

@app.route('/api/reviews', methods=['GET', 'OPTIONS'])
def get_reviews():
    """Public feed of 4-5 star reviews for the pricing page + review schema."""
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    try:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_reviews'
            f'?stars=gte.4&select=stars,name,text,created_at'
            f'&order=created_at.desc&limit=12',
            headers=SUPABASE_HEADERS, timeout=10
        )
        reviews = r.json() if r.status_code == 200 else []
        if not isinstance(reviews, list):
            reviews = []
        return cors(jsonify({'ok': True, 'reviews': reviews}))
    except Exception as e:
        return cors(jsonify({'ok': False, 'error': str(e)})), 500

# ─── EMAIL OCTOPUS WELCOME ────────────────────────────────────────────────────

@app.route('/api/email/subscribe', methods=['POST', 'OPTIONS'])
def subscribe():
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    try:
        body = request.get_json()
        email = body.get('email', '').lower().strip()
        name = body.get('name', '')
        if not email:
            return cors(jsonify({'error': 'No email'})), 400

        r = requests.post(
            f'https://emailoctopus.com/api/2.0/lists/{EO_LIST_ID}/contacts',
            headers={'Authorization': f'Bearer {EO_API_KEY}'},
            json={
                'email_address': email,
                'fields': {'FirstName': name},
                'status': 'subscribed'
            },
            timeout=10
        )
        print(f'[subscribe] EmailOctopus said {r.status_code}: {r.text[:200]}')

        print(f'Welcome email queued for {email}')
        return cors(jsonify({'ok': True}))
    except Exception as e:
        return cors(jsonify({'error': str(e)})), 500

@app.route('/api/email/debug', methods=['GET'])
def email_debug():
    """Visit this URL directly in a browser — no logs needed. Shows exactly
    what's wrong with the EmailOctopus connection in plain text on screen.
    Temporary diagnostic route, safe to remove once this is sorted."""
    lines = []
    lines.append(f'EO_API_KEY set: {bool(EO_API_KEY)}')
    lines.append(f'EO_API_KEY starts with: {EO_API_KEY[:6]}... (length {len(EO_API_KEY)})' if EO_API_KEY else 'EO_API_KEY is EMPTY — this is the problem. Not set in Render.')
    lines.append(f'EO_LIST_ID: {EO_LIST_ID}')
    lines.append('')

    if not EO_API_KEY:
        return '\n'.join(lines), 200, {'Content-Type': 'text/plain'}

    # Step 0: show every real list actually on this account, with correct IDs —
    # this sidesteps needing to hunt through EmailOctopus's own UI at all.
    try:
        all_lists = requests.get(
            'https://emailoctopus.com/api/2.0/lists',
            headers={'Authorization': f'Bearer {EO_API_KEY}'},
            timeout=10
        )
        lines.append(f'STEP 0 — Your real lists on this account: HTTP {all_lists.status_code}')
        lines.append(f'Response: {all_lists.text[:800]}')
        lines.append('')
        lines.append('>>> Compare the "id" value(s) above against EO_LIST_ID below. If they differ, that mismatch is the whole bug.')
        lines.append('')
    except Exception as e:
        lines.append(f'STEP 0 — FAILED: {e}')

    # Step 1: does this list ID actually exist and is the key valid for it?
    try:
        list_check = requests.get(
            f'https://emailoctopus.com/api/2.0/lists/{EO_LIST_ID}',
            headers={'Authorization': f'Bearer {EO_API_KEY}'},
            timeout=10
        )
        lines.append(f'STEP 1 — Checking the list exists: HTTP {list_check.status_code}')
        lines.append(f'Response: {list_check.text[:300]}')
        lines.append('')
    except Exception as e:
        lines.append(f'STEP 1 — FAILED to even reach EmailOctopus: {e}')
        return '\n'.join(lines), 200, {'Content-Type': 'text/plain'}

    # Step 2: try actually adding a real test contact right now
    test_email = f'ptrain-test-{int(time.time())}@example.com'
    try:
        create_check = requests.post(
            f'https://emailoctopus.com/api/2.0/lists/{EO_LIST_ID}/contacts',
            headers={'Authorization': f'Bearer {EO_API_KEY}'},
            json={'email_address': test_email, 'status': 'subscribed'},
            timeout=10
        )
        lines.append(f'STEP 2 — Creating a real test contact ({test_email}): HTTP {create_check.status_code}')
        lines.append(f'Response: {create_check.text[:500]}')
        lines.append('')
        if create_check.status_code < 300:
            lines.append('SUCCESS — go check EmailOctopus contacts now, this test email should be there.')
        else:
            lines.append('THIS is the real error. Whatever the response above says is exactly why contacts are not appearing.')
    except Exception as e:
        lines.append(f'STEP 2 — FAILED: {e}')

    return '\n'.join(lines), 200, {'Content-Type': 'text/plain'}

# ─── AI GENERATION ────────────────────────────────────────────────────────────

def build_prompt(profile):
    p = profile

    progression_context = ''
    history = p.get('history', [])
    lift_history = p.get('liftHistory', {})
    pbs = p.get('pbs', {})
    plans_completed = p.get('plansCompleted', 0)

    if history or lift_history:
        progression_context = '\n\nPROGRESSION DATA (use this to adapt the programme):\n'

        if history:
            total_sessions = len(history)
            recent_ratings = [h.get('rating', '') for h in history[:10] if h.get('rating')]
            hard_count = sum(1 for r in recent_ratings if 'Hard' in r)
            easy_count = sum(1 for r in recent_ratings if 'Easy' in r)
            good_count = sum(1 for r in recent_ratings if 'Good' in r)
            progression_context += f'Total sessions completed: {total_sessions}\n'
            if recent_ratings:
                progression_context += f'Recent session ratings: {hard_count} Hard, {good_count} Good, {easy_count} Easy (last {len(recent_ratings)} sessions)\n'
                if hard_count > good_count + easy_count:
                    progression_context += 'INSTRUCTION: User is finding sessions hard — keep volume similar, focus on form and technique cues, do not increase intensity aggressively.\n'
                elif easy_count > good_count + hard_count:
                    progression_context += 'INSTRUCTION: User is finding sessions easy — increase intensity, add sets or weight targets, push progressive overload harder.\n'
                else:
                    progression_context += 'INSTRUCTION: User is progressing well — apply standard progressive overload, increase weights by 2.5-5kg on key lifts.\n'

        if pbs:
            pb_list = [f"{ex}: {data.get('weight','?')}" for ex, data in list(pbs.items())[:8] if isinstance(data, dict)]
            progression_context += f'Recent personal bests: {", ".join(pb_list)}\n'
            progression_context += 'INSTRUCTION: Acknowledge these PBs in the coachMessage and build on these strengths.\n'

        if lift_history:
            lift_context = []
            for exercise, entries in list(lift_history.items())[:10]:
                if entries:
                    latest = entries[-1]
                    raw_weight = str(latest.get('weight', 0)).replace('kg', '').strip()
                    try:
                        last_kg = float(raw_weight) if raw_weight else None
                        if last_kg:
                            level = p.get('level', 'Intermediate')
                            if level in ['Beginner']:
                                step = 1.25
                            elif level in ['Intermediate']:
                                step = 2.5
                            else:
                                step = 2.5
                            suggested = round(last_kg + step, 2)
                        else:
                            suggested = None
                    except (ValueError, TypeError):
                        suggested = None
                    if suggested:
                        lift_context.append(f"{exercise}: last {latest['weight']}kg → target {suggested}kg")
            if lift_context:
                progression_context += f'Exercise weight targets based on history:\n' + '\n'.join(lift_context) + '\n'
                progression_context += 'INSTRUCTION: Use these exact weight targets in the programme. This is real data from their previous sessions.\n'

        if plans_completed > 0:
            progression_context += f'Plans previously generated: {plans_completed} — this is NOT their first plan, adapt accordingly.\n'

    return f"""You are PTrAIn, an AI personal trainer and nutrition coach. Built on real PT expertise by Tel Kershaw, certified PT, 8 years at Gymbox. Coach everyone — any age, level, goal, background. Explain WHY behind every decision.

RULES BY GOAL/LEVEL/STYLE (apply strictly):
- Beginner: form focus, light weights, 1.25kg steps, technique notes, avoid highly technical/complex movements (Olympic lifts, advanced plyometrics) until foundational strength and form are established. Keep sessions to 4-6 exercises — fewer moving parts, better technique focus.
- Intermediate: standard progressive overload, 2.5kg steps, comfortable with compound lifts and moderate complexity (supersets, tempo work). 5-7 exercises per session, can start introducing exercise variety and periodization concepts (e.g. varying rep ranges across weeks).
- Advanced/Elite: heavy compounds, 2.5kg steps, deload planning, 6-8+ exercises per session with room for accessory/weak-point work, comfortable with intensity techniques (drop sets, rest-pause, cluster sets, tempo prescriptions) where appropriate to the goal.
- VOLUME RULE: exercise count and session structure must genuinely differ by level, not just the weight numbers — a Beginner and an Advanced plan for the same goal should look structurally different, not identical exercises with different kg labels.
- WEEKLY SPLIT RULE (for plans with 2+ days/week): distribute muscle groups and movement patterns sensibly across the week — don't hammer the same muscle group or movement pattern on consecutive days without adequate recovery. Vary each day's focus meaningfully (e.g. push/pull/legs, upper/lower, or full-body variations) so the week reads as a genuine programme, not repeated near-identical days.
- Weight loss: resistance training as the PRIMARY driver (preserves muscle mass during a calorie deficit — this is what actually protects metabolic rate, not cardio alone), cardio elements as a supplement, deficit nutrition, HIIT finishers
- Rehabilitation: avoid contraindicated movements, mobility first, keep movements conservative — if the injury/condition described is unclear or sounds serious, say so explicitly in the notes and recommend consulting a physiotherapist before progressing
- No equipment: bodyweight only, tempo/volume progression
- Flexibility/Mobility: mobility sequences, longer holds
- Endurance: high reps, circuits, cardio bias, high carb nutrition
- Calisthenics: bodyweight progressions, skill work, rings, handstands
- CrossFit style: WODs, AMRAPs, EMOMs, functional movements, time-based workouts
- Hyrox style: running intervals + functional fitness stations, sled, farmers carry, burpee broad jumps
- Sport Specific: build programme specifically for {p.get('sport','their sport')} — power, agility, speed, sport-specific conditioning and movement patterns
- HIIT/Circuit: short rest, high intensity, metabolic conditioning
- CIRCUIT/WOD STRUCTURE RULE (applies whenever ANY part of the session uses circuits, rounds, AMRAPs, EMOMs, HIIT finishers, or interval formats — this includes CrossFit, Hyrox, HIIT/Circuit sessions, Weight loss HIIT finishers, Endurance circuits, Sport Specific conditioning drills, and any other style where round-based training appears): the exercises array must contain DIFFERENT, VARIED movements in the actual circuit order — never the same exercise name as consecutive entries. A real WOD like "5 rounds of: row 250m, 15 wall balls, 10 burpees" is THREE distinct exercise entries (Row, Wall Balls, Burpees), each with sets:5 representing the round count — not five separate "Row" entries. Put the round/format structure ("5 rounds for time", "AMRAP 20 min — cycle through in order", "EMOM 12: odd min movement A, even min movement B") in that exercise's notes field. A circuit-based day or finisher should typically include 3-8 genuinely different movements, matching how these formats are actually programmed. (Exception: Flexibility/Mobility work — holding the same stretch for multiple timed sets is correct practice, not repetition.)
- FIRST PLAN (no lift history provided): prescribe CONSERVATIVE starting weights scaled to bodyweight and level. Put RPE guidance ("adjust so the last 2 reps feel hard, RPE 7-8") ONLY in the notes field.
- CRITICAL FORMAT RULE: the "weight" field must be ONLY a number with kg (e.g. "40kg") or "BW" for bodyweight moves. NEVER words, sentences, ranges or explanations in the weight field — those belong in notes. A weight field containing text breaks the app.
- WITH lift history: use their actual numbers plus the progression step. Real data beats estimates.
- Diet preference is a HARD constraint: Keto = very low carb meals only, Vegan = no animal products, etc. Never include foods that violate the stated preference.
- NUTRITION CALCULATION: use Sex alongside Age/Weight/Height/activity level to calculate accurate BMR/TDEE and set dailyCalories/protein/carbs/fats — men and women have genuinely different baseline metabolic rates at identical stats, so this matters for accuracy. If Sex is "Prefer not to say", use a sensible midpoint estimate between typical male/female BMR for their stats — never mention the missing data, never ask about it again, just quietly produce a reasonable estimate.
- Always match equipment strictly. Always respect injuries. Nutrition must match goal.

Profile: Name={p.get('name','User')}, Age={p.get('age','?')}, Sex={p.get('sex','Prefer not to say')}, Weight={p.get('weight','?')}kg, Height={p.get('height','?')}cm
Goal: {p.get('goal','General Fitness')} | Level: {p.get('level','Intermediate')} | Style: {p.get('style','Gym / Weights')}{' | Sport: ' + p.get('sport') if p.get('sport') else ''} | Days: {p.get('days','4')}/week | Duration: {p.get('duration','60')}min
Equipment: {p.get('equipment','Full Gym')} | Focus: {p.get('muscles','Full Body')}
Sleep: {p.get('sleep','7')}hrs | Stress: {p.get('stress','Moderate')} | Diet: {p.get('diet','No preference')} | Vibe: {p.get('vibe','Normal')}
Injuries: {p.get('inj','None')} | Allergies: {p.get('allergies','None')}
{progression_context}
CRITICAL: Respond ONLY with valid complete JSON. Be concise — short exercise notes, brief meal descriptions. No markdown, no backticks, no trailing commas:
{{"planName":"string","coachMessage":"1 sentence to {p.get('name','you')} on why this plan is their next step","overview":"2 sentences","weeklyVolume":0,"estimatedCalories":0,"days":[{{"day":"Day 1 - Focus","focus":"string","warmup":"string","exercises":[{{"name":"string","sets":3,"reps":"8-10","weight":"70kg","rest":"90s","notes":"brief why"}}],"cooldown":"string"}}],"nutritionPlan":{{"dailyCalories":0,"protein":0,"carbs":0,"fats":0,"meals":[{{"meal":"Breakfast","foods":"brief","calories":0}},{{"meal":"Lunch","foods":"brief","calories":0}},{{"meal":"Dinner","foods":"brief","calories":0}},{{"meal":"Snacks","foods":"brief","calories":0}}]}},"nutritionTips":["tip1","tip2","tip3"],"progressionScheme":"brief","deloadRecommendation":"brief"}}"""

@app.route('/api/exercises', methods=['GET', 'OPTIONS'])
def exercises():
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    try:
        if not RAPIDAPI_KEY:
            return cors(jsonify({'error': 'RAPIDAPI_KEY not set'})), 500
        limit = request.args.get('limit', '1300')
        r = requests.get(
            f'https://exercisedb.p.rapidapi.com/exercises?limit={limit}&offset=0',
            headers={
                'x-rapidapi-host': 'exercisedb.p.rapidapi.com',
                'x-rapidapi-key': RAPIDAPI_KEY
            },
            timeout=30
        )
        return cors(jsonify(r.json()))
    except Exception as e:
        return cors(jsonify({'error': str(e)})), 500

@app.route('/api/generate', methods=['POST', 'OPTIONS'])
def generate():
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    if not ANTHROPIC_API_KEY:
        return cors(jsonify({'error': {'message': 'ANTHROPIC_API_KEY not set'}})), 500
    # Only our own pages may call the AI endpoint
    if not origin_ok():
        return cors(jsonify({'error': {'message': 'Forbidden'}})), 403
    # Per-IP rate limit so nobody can drain the Anthropic account
    if rate_limited(client_ip()):
        return cors(jsonify({'error': {'message': 'Too many requests — try again in a bit.'}})), 429
    try:
        body = request.get_json()
        email = str(body.get('email', '')).lower().strip()
        # Enforce each user's actual plan quota server-side. Anonymous users
        # (no email — they chose "Skip" on the save-progress screen) have no
        # persistent identity to check, so they rely on the IP rate limit
        # above as their safety net, same as before.
        current_plan, plans_used = ('free', 0)
        if email:
            current_plan, plans_used = get_user_plan_state(email)
            limit = PLAN_LIMITS.get(current_plan, 1)
            if limit is not None and plans_used >= limit:
                return cors(jsonify({'error': {
                    'message': f"You've used all {limit} plan(s) on your {current_plan} plan this period. Upgrade for more.",
                    'code': 'PLAN_LIMIT_REACHED'
                }})), 403

        if body.get('profile'):
            messages = [{'role': 'user', 'content': build_prompt(body['profile'])}]
            system = None
        else:
            messages = body.get('messages', [])
            system = body.get('system')
        payload = {
            # Model is fixed server-side — never trust a client-supplied model
            # name, since that could be used to request a more expensive model.
            'model': 'claude-sonnet-4-6',
            'max_tokens': min(int(body.get('max_tokens', 6000)), 8000),
            'messages': messages,
        }
        if system:
            payload['system'] = system
        anthropic_response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
            },
            json=payload,
            timeout=180,
        )
        result = anthropic_response.json()
        # Only count it against their quota if this was an actual full plan
        # generation (has a profile) that genuinely succeeded — not chat
        # messages, and not a failed/errored call.
        if email and body.get('profile') and anthropic_response.status_code == 200 and not result.get('error'):
            increment_plans_used(email, current_plan, plans_used)
        return cors(jsonify(result))
    except Exception as e:
        return cors(jsonify({'error': {'message': str(e)}})), 500

if __name__ == '__main__':
    t = threading.Thread(target=keep_alive, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

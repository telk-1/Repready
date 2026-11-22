from flask import Flask, request, jsonify, send_from_directory, redirect
import requests
import os
import json
import hmac
import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# static_folder=None: every file that must be public has its own explicit
# route below. Serving BASE_DIR exposed main.py, requirements.txt and .git
app = Flask(__name__, static_folder=None)

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', 'https://ptrain.onrender.com')
SUPABASE_URL = 'https://ysztaecwyjuqchmcgxjr.supabase.co'
# No hardcoded fallback. The old one was a publishable key, which RLS denies on
# every table, so a missing env var used to mean every read and write failed
# quietly instead of failing loudly. It must come from the environment.
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
EO_API_KEY = os.environ.get('EO_API_KEY', '')
EO_LIST_ID = 'badf6dd4-6fa7-11f1-bb46-e1bea4e2db03'
RAPIDAPI_KEY = os.environ.get('RAPIDAPI_KEY', '')
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
# Signing secret for /api/stripe/webhook (the "whsec_..." value Stripe shows
# when the endpoint is created). Without it the webhook accepts nothing.
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
PUSH_SECRET = os.environ.get('PUSH_SECRET', '')
PUSH_CONTACT_EMAIL = os.environ.get('PUSH_CONTACT_EMAIL', 'tel@ptrain.app')

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

def admin_authorised():
    """Gate for the private dashboards and the Stripe setup route.

    Fails CLOSED. If PUSH_SECRET is not configured, nothing is authorised —
    the previous version compared against the literal string '__unset__' when
    the variable was missing, and because this repository is public that
    fallback was effectively a published password. Compared in constant time
    so the secret can't be recovered a character at a time.
    """
    if not PUSH_SECRET:
        return False
    supplied = request.args.get('key', '')
    if not supplied:
        return False
    return hmac.compare_digest(supplied, PUSH_SECRET)


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

# ── State-endpoint abuse control ──────────────────────────────────────────
# /api/state/load and /api/state/save are authorised by an email address and
# nothing else, so anyone holding a customer's address can read or overwrite
# their saved state. Proper authentication is the real fix and is NOT what
# this is; it limits the blast radius until that exists.
#
# A plain per-request rate limit is the wrong tool: phones share carrier NAT
# addresses, so a request cap would throttle real users long before it
# inconvenienced an attacker. The signal that actually separates them is how
# many DIFFERENT people one address asks about — a real person uses one or
# two, someone working through a list uses many. So the cap is on distinct
# emails per IP, and ordinary use is unaffected no matter how often it syncs.
STATE_EMAILS_PER_IP = 8
STATE_EMAIL_WINDOW = 3600
_state_probe = defaultdict(deque)      # ip -> deque of (timestamp, email)
_state_probe_lock = threading.Lock()


# ── CUSTOMER AUTHENTICATION ───────────────────────────────────────────────
# The state endpoints used to accept an email address as proof of identity, so
# anyone holding a customer's address could read their injuries and training
# history or overwrite it. Identity now comes from a token in the
# Authorization header, and the email in a request body is ignored entirely.
#
# Enforcement is tied to whether we can actually send email, because the token
# is delivered by magic link. If PRIVATE_EMAIL_PASSWORD is unset the app cannot
# deliver a link, so requiring one would lock every customer out of their own
# data. Setting that variable turns on email AND enforcement together, with no
# second deploy. Tokens are issued and honoured either way; only the
# requirement is conditional.
TOKEN_TTL_DAYS = 90

_auth_req = defaultdict(deque)          # ip -> request timestamps
_auth_req_email = defaultdict(deque)    # email -> request timestamps
_auth_lock = threading.Lock()
AUTH_REQ_PER_IP = 10                    # magic links per IP per hour
AUTH_REQ_PER_EMAIL = 5                  # per address per hour


def auth_request_limited(ip, email):
    """Throttle magic-link requests, per IP and per address."""
    now = time.time()
    with _auth_lock:
        for store, key, cap in ((_auth_req, ip, AUTH_REQ_PER_IP),
                                (_auth_req_email, email, AUTH_REQ_PER_EMAIL)):
            q = store[key]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= cap:
                return True
        _auth_req[ip].append(now)
        _auth_req_email[email].append(now)
        return False


def issue_token(email):
    """Create a login token for an address. Returns the token, or None."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)
    try:
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/ptrain_tokens',
            headers={**SUPABASE_HEADERS, 'Prefer': 'return=minimal'},
            json={'token': token, 'email': email,
                  'expires_at': expires.isoformat()},
            timeout=10)
        if r.status_code >= 300:
            print(f'[auth] could not store token: {r.status_code}')
            return None
        return token
    except Exception as e:
        print(f'[auth] token store failed: {e}')
        return None


def email_for_token(token):
    """Resolve a token to its owner. None if unknown, revoked or expired.

    This is the only thing that establishes who a request is for. It never
    consults the request body.
    """
    if not token or len(token) > 128:
        return None
    try:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_tokens'
            f'?token=eq.{token}&select=email,expires_at,revoked',
            headers=SUPABASE_HEADERS, timeout=10)
        if r.status_code >= 300:
            return None
        rows = r.json()
        if not rows:
            return None
        row = rows[0]
        if row.get('revoked'):
            return None
        try:
            if datetime.fromisoformat(
                    str(row['expires_at']).replace('Z', '+00:00')) < datetime.now(timezone.utc):
                return None
        except Exception:
            return None
        # best-effort touch; never block the request on it
        try:
            requests.patch(
                f'{SUPABASE_URL}/rest/v1/ptrain_tokens?token=eq.{token}',
                headers={**SUPABASE_HEADERS, 'Prefer': 'return=minimal'},
                json={'last_used': datetime.now(timezone.utc).isoformat()},
                timeout=5)
        except Exception:
            pass
        return (row.get('email') or '').lower().strip()
    except Exception as e:
        print(f'[auth] token lookup failed: {e}')
        return None


def bearer_token():
    """The token on this request, from the Authorization header."""
    h = request.headers.get('Authorization', '')
    if h.startswith('Bearer '):
        return h[7:].strip()
    return ''


def authorised_email(body_email):
    """Who this request is allowed to act as. Returns (email, error_response).

    With a valid token the token's owner wins and `body_email` is ignored, so
    asking for someone else's address achieves nothing. With enforcement off
    (no mail configured) the body address is accepted, which is the behaviour
    that existed before and is no worse than it was.
    """
    tok = bearer_token()
    if tok:
        owner = email_for_token(tok)
        if owner:
            return owner, None
        return None, (cors(jsonify({'error': 'Session expired', 'reauth': True})), 401)
    if AUTH_ENFORCED:
        return None, (cors(jsonify({'error': 'Sign in required', 'reauth': True})), 401)
    return body_email, None


def state_probe_blocked(ip, email):
    """True when this IP has asked about too many different people lately."""
    now = time.time()
    with _state_probe_lock:
        q = _state_probe[ip]
        while q and now - q[0][0] > STATE_EMAIL_WINDOW:
            q.popleft()
        seen = {e for _, e in q}
        if email not in seen and len(seen) >= STATE_EMAILS_PER_IP:
            return True
        q.append((now, email))
        if len(_state_probe) > 5000:       # keep the map from growing forever
            for k in [k for k, v in list(_state_probe.items()) if not v][:1000]:
                _state_probe.pop(k, None)
        return False

# Server-side plan quotas — must match the limits shown in index.html's PLANS
# object exactly, since that's the promise being made to the user. This is
# enforced here (not just in the frontend) so nobody can bypass their plan
# limit by calling /api/generate directly.
PLAN_LIMITS = {'free': 1, 'pro': 10, 'coach': 50, 'elite': None}  # None = unlimited

# ─── WHAT WAS ACTUALLY PAID FOR ───────────────────────────────────────────────
# The tier a customer gets is derived from the Stripe session itself, never from
# the browser. Price IDs are checked first; the amount paid is the fallback so a
# duplicated product in Stripe can't silently downgrade a paying customer.
STRIPE_PRICE_TIERS = {
    # --- CURRENTLY ON SALE (27 Oct 2026) ---
    'price_1UFCZ4LJBOEFuMCRBFpWcdof': 'pro',    # PTrain Pro £9.99/mo
    'price_1UE3sfLJBOEFuMCRXjaEMWcx': 'pro',    # PTrain Pro £9.99/mo (live link)
    'price_1UFCZ4LJBOEFuMCRDbmEcOaX': 'pro',    # Group Coaching £29 — full app access
    'price_1UE3sgLJBOEFuMCRHsPp5NIN': 'pro',    # Group Coaching £29 (live link)
    'price_1UFCZ5LJBOEFuMCRVHoVMKWf': 'elite',  # Coaching with Tel £149 — no caps
    'price_1UE3shLJBOEFuMCRjXa4aYjc': 'elite',  # Coaching with Tel £149 (live link)
    'price_1U3FPeLJBOEFuMCRoPCB5AGL': 'elite',  # Founding Member £79 one-off, for life
    # One-off service, NOT a subscription tier — must not change what they have
    'price_1UFCZ5LJBOEFuMCRvzAJHKos': 'service',  # Plan Review £19
    'price_1UE3siLJBOEFuMCRkTLBNk6Y': 'service',  # Plan Review £19 (live link)
    # --- RETIRED, kept so existing customers still resolve correctly ---
    'price_1TlFNjLJBOEFuMCRPPcb2YZh': 'pro',    # Pro £6.99
    'price_1Tly5zLJBOEFuMCRYAuTclP3': 'pro',    # Pro £6.99 duplicate
    'price_1TjdOhLJBOEFuMCRdfMV2ycl': 'pro',    # Pro £6.99 third
    'price_1TjdPsLJBOEFuMCR4DbYjtJE': 'coach',  # Coach £29
    'price_1TjdU3LJBOEFuMCREb7kVI7v': 'elite',  # Elite £49
    'price_1U3FOgLJBOEFuMCRRQ33rMQG': 'pro',    # Pro Annual £67.10/yr
    'price_1U3FOmLJBOEFuMCRtPWlKmAF': 'coach',  # Coach Annual £278.40/yr
    'price_1U3FOrLJBOEFuMCRutaHZqcb': 'elite',  # Elite Annual £470.40/yr
}
# Fallback when a price ID isn't recognised. 999 and 14900 were MISSING, which
# meant a £9.99 Pro or £149 coaching payment resolved to nothing and the customer
# was told "Purchase not recognised" after their card had been charged.
STRIPE_AMOUNT_TIERS = {999: 'pro', 2900: 'pro', 14900: 'elite', 7900: 'elite',
                       9900: 'elite',  # legacy £99 coaching — price still live on the
                                       # account; without this a legacy customer paying
                                       # £99 is told 'Purchase not recognised'.
                       1900: 'service',
                       699: 'pro', 4900: 'elite',
                       6710: 'pro', 27840: 'coach', 47040: 'elite'}

def fetch_stripe_session(session_id):
    """Retrieve a checkout session with its line items expanded.
    Returns (session_dict, error_message)."""
    r = requests.get(
        f'https://api.stripe.com/v1/checkout/sessions/{session_id}',
        params={'expand[]': 'line_items.data.price'},
        auth=(STRIPE_SECRET_KEY, ''),
        timeout=15
    )
    data = r.json()
    if r.status_code != 200:
        return None, (data.get('error') or {}).get('message', f'Stripe returned HTTP {r.status_code}')
    return data, None

def tier_from_session(session):
    """Work out which plan a paid session unlocks. Returns None if unrecognised,
    so an unknown purchase never silently grants a tier."""
    items = ((session.get('line_items') or {}).get('data') or [])
    for item in items:
        price_id = ((item.get('price') or {}).get('id')) or ''
        if price_id in STRIPE_PRICE_TIERS:
            return STRIPE_PRICE_TIERS[price_id]
    for item in items:
        amount = item.get('amount_total')
        if amount in STRIPE_AMOUNT_TIERS:
            return STRIPE_AMOUNT_TIERS[amount]
    amount = session.get('amount_total')
    return STRIPE_AMOUNT_TIERS.get(amount)

def _fetch_existing_state(email):
    """Read a user's saved state. Returns (state_dict, error_message).

    The error is returned rather than swallowed: a caller about to overwrite the
    row must know the difference between "this user has no state" and "I could
    not read their state", because writing on the second one destroys data.
    """
    try:
        lookup = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_users?email=eq.{email}&select=state',
            headers=SUPABASE_HEADERS, timeout=10
        )
        if lookup.status_code >= 300:
            return None, f'Supabase read failed: {lookup.status_code}'
        rows = lookup.json()
        if rows and len(rows) > 0:
            return (rows[0].get('state') or {}), None
        return {}, None
    except Exception as err:
        return None, f'Supabase read error: {err}'


def apply_plan_upgrade(email, plan_tier):
    """Write a verified plan change to Supabase without clobbering the rest of
    the user's saved state. Returns (ok, error_message)."""
    if not email or plan_tier not in PLAN_LIMITS:
        return False, 'Invalid email or plan'
    # ABORT if the existing state can't be read. This previously carried on with
    # an empty dict, so one failed read during an upgrade overwrote the
    # customer's plan, history, PBs and referral source with three fields.
    existing_state, read_err = _fetch_existing_state(email)
    if read_err:
        print(f'[apply_plan_upgrade] aborted for {email}: {read_err}')
        return False, read_err
    existing_state['userPlan'] = plan_tier
    existing_state['plansUsed'] = 0
    existing_state['planPeriodStart'] = datetime.now(timezone.utc).isoformat()
    existing_state['subStatus'] = 'active'
    r = requests.post(
        f'{SUPABASE_URL}/rest/v1/ptrain_users',
        headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
        json={'email': email, 'state': existing_state, 'updated_at': 'now()'},
        timeout=10
    )
    if r.status_code >= 300:
        print(f'[apply_plan_upgrade] Supabase write failed for {email}: {r.status_code} {r.text}')
        return False, f'Supabase write failed: {r.status_code}'
    return True, None


def apply_plan_downgrade(email, reason):
    """Drop a customer back to the free tier when Stripe says their
    subscription has ended. Returns (ok, error_message).

    Everything else in their state is preserved — their plan, history, PBs and
    referral source all survive, because they are still the same person and may
    resubscribe. `plansUsed` is deliberately NOT reset: the free tier's limit is
    a lifetime cap, so zeroing it would hand every cancelled subscriber a fresh
    free plan on the way out.
    """
    if not email:
        return False, 'No email'
    existing_state, read_err = _fetch_existing_state(email)
    if read_err:
        print(f'[apply_plan_downgrade] aborted for {email}: {read_err}')
        return False, read_err
    previous = existing_state.get('userPlan', 'free')
    if previous == 'free':
        return True, None          # already free, nothing to do
    existing_state['userPlan'] = 'free'
    existing_state['subStatus'] = reason
    existing_state['downgradedAt'] = datetime.now(timezone.utc).isoformat()
    existing_state['previousPlan'] = previous
    existing_state.pop('planPeriodStart', None)
    r = requests.post(
        f'{SUPABASE_URL}/rest/v1/ptrain_users',
        headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
        json={'email': email, 'state': existing_state, 'updated_at': 'now()'},
        timeout=10
    )
    if r.status_code >= 300:
        print(f'[apply_plan_downgrade] Supabase write failed for {email}: {r.status_code}')
        return False, f'Supabase write failed: {r.status_code}'
    print(f'[webhook] downgraded {email} from {previous} to free ({reason})')
    return True, None


def verify_stripe_signature(payload, sig_header, tolerance=300):
    """Verify Stripe's Stripe-Signature header over the RAW request body.

    Returns (ok, error_message). Fails CLOSED: with no signing secret
    configured, nothing is accepted, because an unauthenticated webhook is a
    way for anyone on the internet to grant or revoke subscriptions.

    Implements the documented scheme — HMAC-SHA256 over "<timestamp>.<body>" —
    with a constant-time compare and a timestamp tolerance so a captured
    request can't be replayed later.
    """
    if not STRIPE_WEBHOOK_SECRET:
        return False, 'STRIPE_WEBHOOK_SECRET not set'
    if not sig_header:
        return False, 'Missing Stripe-Signature header'
    timestamp, signatures = None, []
    for part in sig_header.split(','):
        if '=' not in part:
            continue
        k, v = part.split('=', 1)
        k, v = k.strip(), v.strip()
        if k == 't':
            timestamp = v
        elif k == 'v1':
            signatures.append(v)
    if not timestamp or not signatures:
        return False, 'Malformed Stripe-Signature header'
    try:
        ts = int(timestamp)
    except ValueError:
        return False, 'Bad timestamp in signature'
    age = abs(time.time() - ts)
    if age > tolerance:
        return False, f'Signature timestamp outside tolerance ({int(age)}s)'
    signed = timestamp.encode() + b'.' + payload
    expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
    for candidate in signatures:
        if hmac.compare_digest(expected, candidate):
            return True, None
    return False, 'Signature mismatch'


def tier_from_subscription(sub):
    """Which tier a subscription's price grants. None if unrecognised."""
    for item in ((sub.get('items') or {}).get('data') or []):
        price_id = ((item.get('price') or {}).get('id')) or ''
        if price_id in STRIPE_PRICE_TIERS:
            return STRIPE_PRICE_TIERS[price_id]
    for item in ((sub.get('items') or {}).get('data') or []):
        amount = (item.get('price') or {}).get('unit_amount')
        if amount in STRIPE_AMOUNT_TIERS:
            return STRIPE_AMOUNT_TIERS[amount]
    return None


def email_for_stripe_customer(customer_id):
    """Resolve a Stripe customer id to the email we key users on."""
    if not customer_id or not STRIPE_SECRET_KEY:
        return ''
    try:
        r = requests.get(
            f'https://api.stripe.com/v1/customers/{customer_id}',
            auth=(STRIPE_SECRET_KEY, ''), timeout=15
        )
        if r.status_code != 200:
            print(f'[webhook] could not read customer {customer_id}: HTTP {r.status_code}')
            return ''
        return (r.json().get('email') or '').lower().strip()
    except Exception as e:
        print(f'[webhook] customer lookup failed for {customer_id}: {e}')
        return ''


# Statuses that mean the customer is no longer entitled to a paid tier.
SUB_STATUS_ENDS_ACCESS = {'canceled', 'unpaid', 'incomplete_expired'}


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

def _refine_already_used(email):
    """Has this person already taken their one free plan refinement?"""
    try:
        lookup = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_users?email=eq.{email}&select=state',
            headers=SUPABASE_HEADERS, timeout=10
        )
        rows = lookup.json()
        existing_state = (rows[0].get('state') or {}) if rows else {}
        return bool(existing_state.get('refineUsed'))
    except Exception as e:
        print(f'[refine] lookup failed for {email}: {e}')
        # On a lookup failure, charge it rather than hand out unlimited free
        # rebuilds — failing closed costs one plan, failing open costs the quota.
        return True


def _mark_refine_used(email, current_plan, current_used):
    """Record that the free refinement is spent, preserving the rest of their
    state (same merge-not-overwrite pattern as increment_plans_used)."""
    try:
        lookup = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_users?email=eq.{email}&select=state',
            headers=SUPABASE_HEADERS, timeout=10
        )
        rows = lookup.json()
        existing_state = (rows[0].get('state') or {}) if rows else {}
        existing_state['userPlan'] = current_plan
        existing_state['plansUsed'] = current_used
        existing_state['refineUsed'] = True
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/ptrain_users',
            headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
            json={'email': email, 'state': existing_state, 'updated_at': 'now()'},
            timeout=10
        )
        if r.status_code >= 300:
            print(f'[refine] Supabase rejected the write for {email}: {r.status_code} {r.text[:200]}')
    except Exception as e:
        print(f'[refine] could not mark refine used for {email}: {e}')


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
    resp_headers = {'Cache-Control': 'no-store, must-revalidate', 'Pragma': 'no-cache'}
    try:
        with open(os.path.join(BASE_DIR, 'index.html'), 'r', encoding='utf-8') as f:
            html = f.read()

        # The version stamp exists so a deploy can be confirmed at a glance.
        # It was a hard-coded 'v10' and had not moved in months of releases,
        # which made the one tool for checking a release actively misleading.
        _rev = (os.environ.get('RENDER_GIT_COMMIT')
                or os.environ.get('PTRAIN_RELEASE') or 'dev')[:7]
        html = html.replace('>v10</span>', '>' + _rev + '</span>')

        r = requests.get(
            # select only what the schema uses. This previously selected every
            # column, which pulled reviewers' email addresses into the page
            # handler even though the markup never used them.
            f'{SUPABASE_URL}/rest/v1/ptrain_reviews'
            f'?stars=gte.4&select=stars,name,text,created_at'
            f'&order=created_at.desc&limit=20',
            headers=SUPABASE_HEADERS, timeout=5
        )
        reviews = r.json() if r.status_code == 200 else []
        if isinstance(reviews, list) and len(reviews) > 0:
            avg = round(sum(rv.get('stars', 5) for rv in reviews) / len(reviews), 1)
            schema = {
                "@context": "https://schema.org",
                "@type": "SoftwareApplication",
                "name": "PTrain",
                "url": "https://ptrain.app",
                "applicationCategory": "HealthApplication",
                "operatingSystem": "Web, iOS, Android",
                "aggregateRating": {"@type": "AggregateRating", "ratingValue": str(avg), "ratingCount": len(reviews), "bestRating": "5"},
                "review": [
                    {
                        "@type": "Review",
                        "reviewRating": {"@type": "Rating", "ratingValue": rv.get('stars', 5), "bestRating": "5"},
                        "author": {"@type": "Person", "name": rv.get('name') or "PTrain member"},
                        "reviewBody": rv.get('text', '')
                    } for rv in reviews[:5]
                ]
            }
            script_tag = f'<script type="application/ld+json">{json.dumps(schema)}</script></head>'
            html = html.replace('</head>', script_tag, 1)
        resp = app.response_class(html, mimetype='text/html')
        for k, v in resp_headers.items():
            resp.headers[k] = v
        return resp
    except Exception as e:
        print(f'[home] server-side review schema failed, falling back to static file: {e}')
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
w.innerHTML='<div style=\\"font-weight:900;font-size:19px;letter-spacing:-.02em;margin-bottom:4px\\">PTrain</div>'+
'<div style=\\"font-weight:800;font-size:16px;color:#fff;margin-bottom:6px\\">A personal trainer in your pocket.</div>'+
'<div style=\\"font-size:13px;color:#c8c8d0;line-height:1.5;margin-bottom:14px\\">Full training plan + nutrition plan built around you in minutes. First week free, no card.</div>'+
'<a href=\\"https://ptrain.app?ref='+ref+'\\" style=\\"display:block;text-align:center;background:#ff5a1f;color:#fff;font-weight:800;font-size:14px;padding:12px;border-radius:11px;text-decoration:none\\">Build my free plan →</a>';
s.parentNode.insertBefore(w,s);
})();"""
    return js, 200, {'Content-Type': 'application/javascript', 'Access-Control-Allow-Origin': '*'}

@app.route('/embed')
def embed_page():
    html = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Embed PTrain on your site</title><style>body{background:#0a0a0d;color:#fff;font-family:-apple-system,sans-serif;max-width:640px;margin:0 auto;padding:48px 24px;line-height:1.6}h1{font-weight:900;letter-spacing:-.03em}h1 span{color:#ff5a1f}code{display:block;background:#16161c;border:1px solid rgba(245,245,240,.1);border-radius:12px;padding:16px;font-size:13px;color:#ff8a4f;word-break:break-all;margin:16px 0}p{color:#c8c8d0}</style></head><body><h1>Put PTrain <span>on your site.</span></h1><p>PTs, gyms, bloggers — paste this one line anywhere in your page. A PTrain card appears, your visitors get a free week's plan, and if you're an affiliate your ref is tracked automatically.</p><code>&lt;script src="https://ptrain.app/embed.js" data-ref="YOURNAME"&gt;&lt;/script&gt;</code><p>Change <b>YOURNAME</b> to your affiliate handle. Questions: tel@ptrain.app</p></body></html>"""
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
<url><loc>https://ptrain.app/terms</loc><priority>0.3</priority></url>
</urlset>'''
    return xml, 200, {'Content-Type': 'application/xml'}

@app.route('/llms.txt')
def llms():
    txt = """# PTrain
> AI personal trainer app built by Tel Kershaw, a certified PT with 8 years at Gymbox London. Generates complete personalised training programmes and nutrition plans in minutes.

- Consumer app: https://ptrain.app — first week's programme and nutrition plan free, Pro £9.99/month
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

@app.route('/api/push/key')
def push_key():
    """The public half of the VAPID pair is meant to be public — the browser
    needs it to create a subscription."""
    return cors(jsonify({'key': VAPID_PUBLIC_KEY}))

@app.route('/sw.js')
def service_worker():
    """Must be served from the root path — a service worker can only control
    pages at or below its own scope, so /static/sw.js would control nothing."""
    resp = send_html('sw.js')
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/api/push/subscribe', methods=['POST', 'OPTIONS'])
def push_subscribe():
    """Store a browser push subscription against an email so we can nudge them
    later. Safe to call repeatedly — it upserts on the endpoint."""
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    try:
        body = request.get_json() or {}
        sub = body.get('subscription') or {}
        endpoint = str(sub.get('endpoint', '')).strip()
        email = str(body.get('email', '')).lower().strip()
        if not endpoint.startswith('http'):
            return cors(jsonify({'ok': False, 'error': 'Invalid subscription'})), 400
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/ptrain_push',
            headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
            json={'endpoint': endpoint, 'email': email, 'subscription': sub, 'updated_at': 'now()'},
            timeout=10
        )
        if r.status_code >= 300:
            print(f'[push_subscribe] Supabase write failed: {r.status_code} {r.text[:200]}')
            return cors(jsonify({'ok': False, 'error': 'Could not save subscription'})), 502
        return cors(jsonify({'ok': True}))
    except Exception as e:
        return cors(jsonify({'ok': False, 'error': str(e)})), 500

@app.route('/api/push/send', methods=['POST'])
def push_send():
    """Fire a notification to everyone subscribed. Protected by PUSH_SECRET so
    only your own scheduler can call it — never expose this to the browser.
    Point cron-job.org at it with the secret in the JSON body."""
    try:
        body = request.get_json() or {}
        if not PUSH_SECRET or body.get('secret') != PUSH_SECRET:
            return jsonify({'ok': False, 'error': 'Forbidden'}), 403
        if not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
            return jsonify({'ok': False, 'error': 'VAPID keys not set'}), 500
        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            return jsonify({'ok': False, 'error': 'pywebpush not installed — add it to requirements.txt'}), 500

        title = body.get('title') or 'PTrain'
        message = body.get('body') or 'Your next session is waiting.'
        url = body.get('url') or '/'
        sent, dead_count = _push_to_all(title, message, url)
        return jsonify({'ok': True, 'sent': sent, 'removed': dead_count})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

def _push_to_all(title, message, url='/'):
    """Send one notification to every subscriber. Returns (sent, removed).
    Used by both the /api/push/send route and the internal scheduler."""
    from pywebpush import webpush, WebPushException
    if not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
        return 0, 0
    if True:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_push?select=endpoint,subscription',
            headers=SUPABASE_HEADERS, timeout=15
        )
        rows = r.json() if r.status_code == 200 else []
        sent, dead = 0, []
        payload = json.dumps({'title': title, 'body': message, 'url': url})
        for row in rows:
            sub = row.get('subscription')
            if not sub:
                continue
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={'sub': f'mailto:{PUSH_CONTACT_EMAIL}'}
                )
                sent += 1
            except WebPushException as e:
                # 404/410 means the browser dropped it — stop trying that one.
                status = getattr(getattr(e, 'response', None), 'status_code', None)
                if status in (404, 410):
                    dead.append(row.get('endpoint'))
            except Exception as e:
                print(f'[push_send] failed for one subscriber: {e}')
        for endpoint in dead:
            try:
                requests.delete(
                    f'{SUPABASE_URL}/rest/v1/ptrain_push?endpoint=eq.{endpoint}',
                    headers=SUPABASE_HEADERS, timeout=10
                )
            except Exception:
                pass
        return sent, len(dead)

# ─── EMAIL SEQUENCES ─────────────────────────────────────────────────────────
# Sent directly over SMTP from tel@ptrain.app. No third-party sending service,
# nothing to paste into a dashboard, and the copy lives here in the repo where
# it can be edited like any other code.
#
# Needs one Render env var: PRIVATE_EMAIL_PASSWORD (the mailbox password).

import smtplib
from email.message import EmailMessage

SMTP_HOST = os.environ.get('SMTP_HOST', 'mail.privateemail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '465'))
SMTP_USER = os.environ.get('SMTP_USER', 'tel@ptrain.app')
SMTP_PASS = os.environ.get('PRIVATE_EMAIL_PASSWORD', '')

# Requiring a sign-in token is only safe once we can deliver one. Setting the
# mailbox password switches on email AND authentication together.
AUTH_ENFORCED = bool(SMTP_PASS)

def _first_name(state_blob):
    raw = ((state_blob.get('plan') or {}).get('formSnapshot') or {}).get('name') or ''
    raw = str(raw).strip()
    if not raw or raw.lower() in ('athlete', 'user'):
        return ''
    return raw.split()[0][:24]

def _plan_facts(state_blob):
    """Whatever we honestly know about this person's plan."""
    snap = ((state_blob.get('plan') or {}).get('formSnapshot') or {})
    style = str(snap.get('style') or '').strip()
    return {
        'name': _first_name(state_blob),
        'goal': str(snap.get('goal') or '').strip(),
        'equipment': str(snap.get('equipment') or '').strip(),
        'days': snap.get('days') or '',
        'style': style,
        'fast_cycle': any(k in style.lower() for k in ('crossfit', 'hyrox', 'hiit', 'circuit')),
        'sessions': len(state_blob.get('history') or []),
        'has_plan': bool((state_blob.get('plan') or {}).get('days')),
    }

def _greeting(f):
    return f'Hi {f["name"]},' if f['name'] else 'Hi,'

SIGNOFF = "\n\nTel\nptrain.app"
FULL_SIGNOFF = ("\n\nTel Kershaw\nCertified PT · 8 years at Gymbox London · REPs Registered"
                "\nptrain.app")

def _compose(step, f):
    """Each email is written from what this person actually did. Anything we
    don't know is simply left out rather than guessed at."""
    g = _greeting(f)
    kit = f['equipment'].lower() if f['equipment'] else ''
    goal = f['goal'].lower() if f['goal'] else ''

    if step == 'welcome':
        opener = f"{g}\n\nThanks for signing up to PTrain."
        if f['has_plan'] and goal:
            opener += f"\n\nI can see your plan's built — {goal}"
            if kit:
                opener += f", around {kit}"
            opener += ". Good start."
        return ('Welcome to PTrain', opener +
                "\n\nI'm Tel. Personal trainer, eight years at Gymbox in London, REPs registered. "
                "I built this because most people don't need another gym membership — they need to know "
                "what to do when they get there."
                "\n\nTwo things worth doing today."
                "\n\nAdd it to your home screen. In Safari, tap share, then Add to Home Screen. "
                "It then works like a normal app, offline included."
                "\n\nTick your sets off as you train, and put the weight in. Four seconds a set, and it's "
                "what makes your next plan better than this one."
                "\n\nAny questions, just reply. It comes straight to me." + SIGNOFF)

    if step == 'nudge_start':
        if f['has_plan']:
            body = (f"{g}\n\nYour plan's sitting there ready. First session is the hard one — "
                    "after that it's just turning up."
                    "\n\nIf something's in the way, tell me what it is and I'll sort it. "
                    "Wrong equipment, no time, not sure where to start — reply and say. I read them all.")
        else:
            body = (f"{g}\n\nYou signed up but haven't built a plan yet."
                    "\n\nIt takes about ninety seconds. Your goal, your level, and whatever kit you can "
                    "actually get to — a full gym, a couple of dumbbells, or two resistance bands in the "
                    "front room. It builds around what you've got."
                    "\n\nptrain.app")
        return ("Haven't started yet?", body + SIGNOFF)

    if step == 'logging':
        return ('The bit most people skip',
                f"{g}\n\nQuick one, and it's the difference between people who progress and people who "
                "collect plans."
                "\n\nTick your sets off as you do them, and put the weight in."
                "\n\nNext time that movement comes round, the app shows what you lifted last time and "
                "suggests where to go next. It spots a personal best on its own. And when your next block "
                "builds, it builds on real numbers instead of a guess."
                "\n\nWithout it you've got a good plan. With it you've got a plan that knows you." + SIGNOFF)

    if step == 'adapt':
        return ("When the plan doesn't fit real life",
                f"{g}\n\nNo plan survives a real week."
                "\n\nThirty-five minutes instead of an hour. A machine taken. A shoulder complaining."
                "\n\nTwo things to know. Hit Swap on any exercise and you'll get alternatives that do the "
                "same job — same movement pattern, so the session still works. And Coach Chat knows your "
                "plan, your kit and your injuries, so \"I've only got 30 minutes, what do I cut?\" gets an "
                "answer for your actual session, not a general article."
                "\n\nThat's part of Pro." + SIGNOFF)

    if step == 'progress':
        s = f['sessions']
        if s >= 3:
            body = (f"{g}\n\nYou've logged {s} sessions. Most people who download a training app never "
                    "get past two, so that's worth saying out loud."
                    "\n\nYour plan was written to run four weeks. When it's done, the next one picks up "
                    "from what you've actually lifted rather than starting again.")
        else:
            body = (f"{g}\n\nYou're a few weeks in."
                    "\n\nIf it hasn't clicked yet, that's usually the plan not fitting your week rather "
                    "than you. Reply and tell me what's getting in the way — I'll tell you what I'd change.")
        return ('How it\'s going', body + SIGNOFF)

    if step == 'winback':
        if f['sessions'] >= 3:
            body = (f"{g}\n\nIt's been a while, and I'm not going to pretend I haven't noticed."
                    f"\n\nYou logged {f['sessions']} sessions before life got in the way. That work "
                    "hasn't gone anywhere — every weight you put in is still saved, and a new plan would "
                    "start from those numbers rather than from scratch."
                    "\n\nThat's the bit people don't realise. Coming back isn't starting again."
                    "\n\nAnd if you'd rather tell me why you stopped, reply and say. Genuinely useful "
                    "to me, and I read every one.")
        else:
            body = (f"{g}\n\nYou signed up a while back and it never really got going."
                    "\n\nNo lecture. But if it was the plan that didn't fit — wrong equipment, wrong "
                    "days, too much — that's a two-minute fix, and I'd rather fix it than have you think "
                    "training isn't for you."
                    "\n\nReply and tell me what got in the way, or build a fresh one at ptrain.app.")
        return ('Your numbers are still here', body + SIGNOFF)

    if step == 'block_end':
        return ('Your block is nearly done',
                f"{g}\n\nYour plan was built to run four weeks and it's nearly there."
                "\n\nThe next block is where it gets interesting — it builds on your logged numbers, "
                "pushes the loads on, and keeps the structure that's been working. That's Pro: a new block "
                "whenever you want one, nutrition with every plan, and Coach Chat."
                "\n\nThere's also a Founding Member option — pay once, keep it for life."
                "\n\nAnd if you want the bit the app can't do — someone watching your form, adjusting it "
                "weekly, chasing you when you go quiet — that's me, direct."
                "\n\nptrain.app" + FULL_SIGNOFF)

    return None

# (day_offset, step_key). Varied formats go stale faster, so their block-end
# prompt lands earlier — that's programming, not sales.
SEQUENCE_STANDARD = [(0, 'welcome'), (2, 'nudge_start'), (5, 'logging'),
                     (11, 'adapt'), (18, 'progress'), (25, 'block_end'), (45, 'winback')]
SEQUENCE_FAST = [(0, 'welcome'), (2, 'nudge_start'), (4, 'logging'),
                 (8, 'adapt'), (13, 'progress'), (18, 'block_end'), (38, 'winback')]

import hashlib

def _unsub_token(email):
    seed = (PUSH_SECRET or 'ptrain') + email.strip().lower()
    return hashlib.sha256(seed.encode()).hexdigest()[:20]

@app.route('/unsubscribe')
def unsubscribe():
    email = (request.args.get('e') or '').strip().lower()
    token = request.args.get('t') or ''
    if not email or token != _unsub_token(email):
        return 'Invalid link', 400
    try:
        r = requests.get(f'{SUPABASE_URL}/rest/v1/ptrain_users?email=eq.{email}&select=state',
                         headers=SUPABASE_HEADERS, timeout=10)
        rows = r.json() if r.status_code == 200 else []
        st = (rows[0].get('state') or {}) if rows else {}
        st['emailsOff'] = True
        requests.post(f'{SUPABASE_URL}/rest/v1/ptrain_users?on_conflict=email',
                      headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates'},
                      json={'email': email, 'state': st}, timeout=10)
    except Exception:
        pass
    return """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<body style="background:#0a0a0d;color:#f5f5f0;font-family:-apple-system,sans-serif;padding:40px 22px;text-align:center">
<div style="font-weight:900;font-size:20px;margin-bottom:8px">Done</div>
<div style="color:#8b8b95;font-size:14px;line-height:1.6;max-width:380px;margin:0 auto">
You won't get any more emails from me. Your plan and everything you've logged is untouched —
carry on using the app whenever you like.</div>
<a href="https://ptrain.app" style="display:inline-block;margin-top:22px;color:#ff5a1f;font-weight:700;
text-decoration:none;font-size:14px">Back to PTrain</a></body>"""

def _send_email(to_addr, subject, body):
    if not SMTP_PASS:
        raise RuntimeError('PRIVATE_EMAIL_PASSWORD not set')
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = f'Tel at PTrain <{SMTP_USER}>'
    msg['To'] = to_addr
    msg['Reply-To'] = SMTP_USER
    footer = (f"\n\n—\nYou're getting this because you signed up at ptrain.app."
              f"\nStop these emails: https://ptrain.app/unsubscribe?e={to_addr}&t={_unsub_token(to_addr)}")
    msg.set_content(body + footer)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)

def _email_worker():
    """Hourly. Picks the right email for where each person actually is —
    their pace, their plan, their sessions — and never sends the same one
    twice. People who opted out are skipped for good."""
    time.sleep(120)
    while True:
        try:
            if SMTP_PASS:
                r = requests.get(
                    f'{SUPABASE_URL}/rest/v1/ptrain_users?select=email,state,created_at',
                    headers=SUPABASE_HEADERS, timeout=20)
                users = r.json() if r.status_code == 200 else []
                today = datetime.now(UK_TZ).date()
                for u in users:
                    email = (u.get('email') or '').strip()
                    if not email or '@' not in email:
                        continue
                    st = u.get('state') or {}
                    if st.get('emailsOff'):
                        continue
                    sent = st.get('emailsSent') or []
                    try:
                        joined = datetime.fromisoformat(
                            (u.get('created_at') or '').replace('Z', '+00:00')).date()
                    except Exception:
                        continue
                    age = (today - joined).days
                    f = _plan_facts(st)
                    seq = SEQUENCE_FAST if f['fast_cycle'] else SEQUENCE_STANDARD

                    for day, key in seq:
                        if age < day or key in sent:
                            continue
                        # don't tell someone to start when they already have
                        if key == 'nudge_start' and f['sessions'] > 0:
                            sent.append(key)
                            continue
                        composed = _compose(key, f)
                        if not composed:
                            sent.append(key)
                            continue
                        subject, body = composed
                        try:
                            _send_email(email, subject, body)
                            sent.append(key)
                            st['emailsSent'] = sent
                            requests.post(
                                f'{SUPABASE_URL}/rest/v1/ptrain_users?on_conflict=email',
                                headers={**SUPABASE_HEADERS,
                                         'Prefer': 'resolution=merge-duplicates'},
                                json={'email': email, 'state': st}, timeout=15)
                            print(f'[email] {key} -> {email}')
                        except Exception as e:
                            print(f'[email] failed {key} -> {email}: {e}')
                        break  # one per person per pass
        except Exception as e:
            print(f'[email_worker] {e}')
        time.sleep(3600)

# ─── REMINDER SCHEDULER ──────────────────────────────────────────────────────
# Runs inside the app, so there is nothing to set up and nothing to pay for.
# Twice a week, at a sensible hour. Messages rotate so it never reads like a
# robot nagging, and it never fires more than once per slot.
# UK local time, not UTC — otherwise every reminder lands an hour late
# through British Summer Time.
try:
    from zoneinfo import ZoneInfo
    UK_TZ = ZoneInfo('Europe/London')
except Exception:
    UK_TZ = timezone.utc

# Messages rotate week to week so it never reads like the same robot.
# Nothing here guilt-trips: people who feel told off delete the app.
# Sunday sets the week up. Wednesday catches it before it slips.
SUNDAY_MESSAGES = [
    ('Week ahead', 'Pick your training days now and Monday looks after itself.'),
    ('New week', 'Two minutes to plan it. Beats deciding at 7am on Monday.'),
    ('Sunday', "What's your first session this week? Get it booked in."),
    ('Fresh week', 'Same plan, heavier than last week. That\'s the whole game.'),
]
WEDNESDAY_MESSAGES = [
    ('Midweek', 'One session between now and Sunday keeps the week honest.'),
    ('Still time', 'Two sessions beats none. Tonight counts.'),
    ('Halfway', 'Get one in tonight and it\'s been a good week.'),
    ('Wednesday', 'Twenty minutes is a session. It doesn\'t have to be perfect.'),
]
_reminder_last = {}

def _next_session_name(state_blob):
    """The name of the next day they haven't finished, e.g. 'Push A'."""
    try:
        plan = state_blob.get('plan') or {}
        done = state_blob.get('completedDays') or {}
        for i, d in enumerate(plan.get('days') or []):
            key = f"{plan.get('id', '')}_{i}"
            if not any(k.endswith(f'_{i}') and v for k, v in done.items()):
                return d.get('day') or f'Day {i + 1}'
    except Exception:
        pass
    return None

def _days_since_last_session(state_blob):
    """History dates are written by the browser as dd/mm/yyyy."""
    try:
        hist = state_blob.get('history') or []
        if not hist:
            return None
        last = hist[0].get('date')
        d = datetime.strptime(last, '%d/%m/%Y').date()
        return (datetime.now(UK_TZ).date() - d).days
    except Exception:
        return None

def _personal_message(state_blob, fallback):
    """Build a message from what this person has actually done. Falls back
    to the generic line if we know nothing useful about them. Returns None
    to mean 'send nothing' — someone who trained yesterday does not need
    a reminder, and messaging them anyway is how you get switched off."""
    try:
        if state_blob is None:
            return fallback
        sessions = len(state_blob.get('history') or [])
        gap = _days_since_last_session(state_blob)
        nxt = _next_session_name(state_blob)
        has_plan = bool((state_blob.get('plan') or {}).get('days'))

        if not has_plan:
            return ('Your plan is waiting', 'Sixty seconds and you have got a full week — training and food.')

        if sessions == 0:
            return ('Day one', f"{nxt} is first up. Thirty-five minutes and it is done." if nxt
                    else 'Your plan is ready when you are.')

        if gap is None:
            return fallback

        if gap <= 2:
            return None  # they are training. Leave them alone.

        if gap <= 6:
            return (f'{nxt} is next' if nxt else 'Next session',
                    f"{gap} days since your last one. Good week so far — keep it going.")

        if gap <= 13:
            return ('Still there?',
                    f"It has been {gap} days. Your plan has not moved — {nxt} whenever you are ready." if nxt
                    else 'Your plan has not moved. Pick it up whenever.')

        return ('No pressure',
                'Everything you logged is still saved. Start where you left off, or build a fresh plan.')
    except Exception:
        return fallback

def _push_personalised(fallback_title, fallback_body):
    """One message per subscriber, written from their own training data."""
    from pywebpush import webpush, WebPushException
    if not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
        return 0, 0, 0
    subs = requests.get(
        f'{SUPABASE_URL}/rest/v1/ptrain_push?select=endpoint,email,subscription',
        headers=SUPABASE_HEADERS, timeout=15
    )
    rows = subs.json() if subs.status_code == 200 else []
    users = requests.get(
        f'{SUPABASE_URL}/rest/v1/ptrain_users?select=email,state',
        headers=SUPABASE_HEADERS, timeout=15
    )
    by_email = {}
    if users.status_code == 200:
        for u in (users.json() or []):
            if u.get('email'):
                by_email[u['email'].strip().lower()] = u.get('state') or {}

    sent, skipped, dead = 0, 0, []
    for row in rows:
        sub = row.get('subscription')
        if not sub:
            continue
        email = (row.get('email') or '').strip().lower()
        msg = _personal_message(by_email.get(email), (fallback_title, fallback_body))
        if msg is None:
            skipped += 1
            continue
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({'title': msg[0], 'body': msg[1], 'url': '/'}),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={'sub': f'mailto:{PUSH_CONTACT_EMAIL}'}
            )
            sent += 1
        except WebPushException as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if status in (404, 410):
                dead.append(row.get('endpoint'))
        except Exception as e:
            print(f'[push_personal] failed for one subscriber: {e}')
    for endpoint in dead:
        try:
            requests.delete(
                f'{SUPABASE_URL}/rest/v1/ptrain_push?endpoint=eq.{endpoint}',
                headers=SUPABASE_HEADERS, timeout=10
            )
        except Exception:
            pass
    return sent, skipped, len(dead)

def _activation_worker():
    """The single most valuable notification this app can send.

    Benchmarks for fitness apps are blunt: activation means finishing a first
    workout, and someone who hasn't done one by day 7 has under a 15% chance of
    ever doing it. A nudge inside the first 24 hours recovers 10-20% of the
    people who stalled. Nothing else in the retention stack pays like this.

    Runs hourly. Finds anyone who generated a plan roughly a day ago and has
    still not logged a single session, sends them ONE message, and records it
    so they are never nudged twice."""
    from pywebpush import webpush, WebPushException
    time.sleep(300)
    while True:
        try:
            if not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
                time.sleep(3600)
                continue
            subs = requests.get(
                f'{SUPABASE_URL}/rest/v1/ptrain_push?select=endpoint,email,subscription',
                headers=SUPABASE_HEADERS, timeout=15
            )
            rows = subs.json() if subs.status_code == 200 else []
            users = requests.get(
                f'{SUPABASE_URL}/rest/v1/ptrain_users?select=email,state,created_at',
                headers=SUPABASE_HEADERS, timeout=15
            )
            by_email = {}
            if users.status_code == 200:
                for u in (users.json() or []):
                    if u.get('email'):
                        by_email[u['email'].strip().lower()] = u

            now = datetime.now(timezone.utc)
            sent = 0
            for row in rows:
                sub = row.get('subscription')
                email = (row.get('email') or '').strip().lower()
                if not sub or not email:
                    continue
                user = by_email.get(email)
                if not user:
                    continue
                st = user.get('state') or {}
                if not isinstance(st, dict):
                    continue
                # Already nudged, or already training — leave them alone.
                if st.get('activationNudged'):
                    continue
                if (st.get('sessionLogs') or st.get('streak') or 0):
                    continue
                if not st.get('plan') and not st.get('plansUsed'):
                    continue
                created = user.get('created_at')
                if not created:
                    continue
                try:
                    made = datetime.fromisoformat(str(created).replace('Z', '+00:00'))
                except Exception:
                    continue
                hours = (now - made).total_seconds() / 3600.0
                # 20-36h window: long enough to have had a go, early enough to matter.
                if not (20 <= hours <= 36):
                    continue
                try:
                    webpush(
                        subscription_info=sub,
                        data=json.dumps({
                            'title': 'Day one is the hard one',
                            'body': "Your first session is short on purpose. Twenty minutes and it's done — I'll build next week off it.",
                            'url': '/'
                        }),
                        vapid_private_key=VAPID_PRIVATE_KEY,
                        vapid_claims={'sub': f'mailto:{PUSH_CONTACT_EMAIL}'}
                    )
                    sent += 1
                    st['activationNudged'] = True
                    requests.post(
                        f'{SUPABASE_URL}/rest/v1/ptrain_users',
                        headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
                        json={'email': email, 'state': st, 'updated_at': 'now()'},
                        timeout=10
                    )
                except WebPushException as e:
                    status = getattr(getattr(e, 'response', None), 'status_code', None)
                    if status in (404, 410):
                        try:
                            requests.delete(
                                f"{SUPABASE_URL}/rest/v1/ptrain_push?endpoint=eq.{row.get('endpoint')}",
                                headers=SUPABASE_HEADERS, timeout=10
                            )
                        except Exception:
                            pass
                except Exception as e:
                    print(f'[activation] failed for one subscriber: {e}')
            if sent:
                print(f'[activation] nudged {sent} people who had not started')
        except Exception as e:
            print(f'[activation] worker error: {e}')
        time.sleep(3600)


def _weekly_digest_worker():
    """Monday 8am: emails Tel the short list of people worth a personal word.
    The machine watches; he only has to read one email a week."""
    time.sleep(300)
    while True:
        try:
            now = datetime.now(UK_TZ)
            if SMTP_PASS and now.weekday() == 0 and now.hour == 8:
                key = now.strftime('%Y-%W')
                if _reminder_last.get('digest') != key:
                    _reminder_last['digest'] = key
                    r = requests.get(
                        f'{SUPABASE_URL}/rest/v1/ptrain_users?select=email,state,created_at',
                        headers=SUPABASE_HEADERS, timeout=20)
                    users = r.json() if r.status_code == 200 else []
                    today = now.date()
                    slipping, winning, stuck, noplan, paying = [], [], [], [], 0
                    for u in users:
                        st = u.get('state') or {}
                        em = u.get('email') or '?'
                        hist = st.get('history') or []
                        n = len(hist)
                        if (st.get('userPlan') or 'free') != 'free':
                            paying += 1
                        gap = None
                        if hist:
                            try:
                                gap = (today - datetime.strptime(
                                    hist[0].get('date'), '%d/%m/%Y').date()).days
                            except Exception:
                                gap = None
                        has_plan = bool((st.get('plan') or {}).get('days'))
                        if not has_plan:
                            noplan.append(em)
                        elif n == 0:
                            stuck.append(em)
                        elif gap is not None and 5 <= gap <= 14:
                            slipping.append(f'{em} — {n} sessions, {gap}d quiet')
                        elif gap is not None and gap <= 4 and n >= 6:
                            winning.append(f'{em} — {n} sessions')

                    def sec(title, rows):
                        if not rows:
                            return ''
                        return f'\n\n{title}\n' + '\n'.join('  ' + str(x) for x in rows[:12])

                    body = (f'Morning Tel.\n\n{len(users)} users, {paying} paying.'
                            + sec('SLIPPING — a message from you lands hardest here:', slipping)
                            + sec('WORTH A COACHING OFFER — these are the £149 conversations:', winning)
                            + sec('BUILT A PLAN, NEVER STARTED — ask what stopped them:', stuck)
                            + sec('SIGNED UP, NO PLAN YET:', noplan)
                            + '\n\nFull list: https://ptrain.app/coach?key=' + (PUSH_SECRET or '')
                            + '\n\nThe app has already nudged everyone automatically. '
                              'This is only the handful worth a human.')
                    try:
                        _send_email(SMTP_USER, 'Who needs you this week', body)
                        print('[digest] sent')
                    except Exception as e:
                        print(f'[digest] failed: {e}')
        except Exception as e:
            print(f'[digest_worker] {e}')
        time.sleep(1800)

def reminder_worker():
    """Sunday and Wednesday at 6pm UK. Twice a week is a nudge; more is
    nagging, and nagging gets notifications switched off for good."""
    time.sleep(60)
    while True:
        try:
            now = datetime.now(UK_TZ)
            pool = None
            if now.weekday() == 6:
                pool = SUNDAY_MESSAGES
            elif now.weekday() == 2:
                pool = WEDNESDAY_MESSAGES
            if pool and now.hour == 18:
                key = now.strftime('%Y-%m-%d')
                if _reminder_last.get('day') != key:
                    _reminder_last['day'] = key
                    week = int(now.strftime('%V'))
                    title, message = pool[week % len(pool)]
                    try:
                        sent, skipped, removed = _push_personalised(title, message)
                        print(f'[reminder] {now:%a %d %b %H:%M} sent={sent} skipped={skipped} removed={removed}')
                    except ImportError:
                        print('[reminder] pywebpush not installed — skipping')
                    except Exception as e:
                        print(f'[reminder] failed: {e}')
        except Exception as e:
            print(f'[reminder_worker] {e}')
        time.sleep(600)

@app.route('/s/<store>')
def store_link(store):
    """Short, printable link for a shop's counter card: ptrain.app/s/harvest
    Redirects into the app carrying the ref, so the existing referral
    plumbing stamps referredBy on the user record as normal."""
    safe = ''.join(c for c in store.lower() if c.isalnum() or c in '-_')[:32]
    return redirect(f'/?ref={safe}', code=302)

@app.route('/admin/config')
def admin_config():
    """Which settings are configured — booleans only, never values.

    There was no way to tell whether something like the mailbox password was
    set without reading it. This reports only whether each one is present, so
    "is email actually switched on?" can be answered without ever exposing a
    secret. Admin-gated like the other dashboards.
    """
    if not admin_authorised():
        return 'Not found', 404
    flags = {
        'ANTHROPIC_API_KEY': bool(ANTHROPIC_API_KEY),
        'SUPABASE_KEY': bool(SUPABASE_KEY),
        'STRIPE_SECRET_KEY': bool(STRIPE_SECRET_KEY),
        'STRIPE_WEBHOOK_SECRET': bool(STRIPE_WEBHOOK_SECRET),
        'PUSH_SECRET': bool(PUSH_SECRET),
        'VAPID_PUBLIC_KEY': bool(VAPID_PUBLIC_KEY),
        'VAPID_PRIVATE_KEY': bool(VAPID_PRIVATE_KEY),
        'EO_API_KEY': bool(EO_API_KEY),
        'RAPIDAPI_KEY': bool(RAPIDAPI_KEY),
        'PRIVATE_EMAIL_PASSWORD': bool(SMTP_PASS),
    }
    consequences = []
    if not flags['PRIVATE_EMAIL_PASSWORD']:
        consequences.append('Email engine OFF: no welcome, nudge, inactivity or '
                            'block-end emails, and no Monday digest.')
    if not flags['STRIPE_WEBHOOK_SECRET']:
        consequences.append('Webhook OFF: cancellations and failed payments will '
                            'not revoke access, and a customer who pays without '
                            'returning to the app will not get their tier.')
    if not flags['SUPABASE_KEY']:
        consequences.append('Database OFF: nothing saves or loads.')
    if not flags['STRIPE_SECRET_KEY']:
        consequences.append('Payments OFF: purchases cannot be verified.')
    if not flags['ANTHROPIC_API_KEY']:
        consequences.append('Plan generation OFF.')
    rows = ''.join(
        f'<tr><td>{k}</td><td style="color:{"#4ade80" if v else "#f87171"};font-weight:700">'
        f'{"configured" if v else "NOT SET"}</td></tr>' for k, v in flags.items())
    warn = ''.join(f'<li>{c}</li>' for c in consequences)
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>PTrain config</title></head>'
            f'<body style="background:#0a0a0d;color:#f5f5f0;font-family:-apple-system,sans-serif;'
            f'padding:24px;max-width:560px;margin:0 auto;line-height:1.6">'
            f'<h1 style="color:#ff5a1f;font-size:22px">Configuration</h1>'
            f'<p style="color:#8b8b95;font-size:13px">Presence only. No values are shown or logged.</p>'
            f'<table style="width:100%;border-collapse:collapse;font-size:13px">{rows}</table>'
            + (f'<h2 style="color:#ff8a4f;font-size:16px;margin-top:24px">What is switched off</h2>'
               f'<ul style="color:#e4e4e8;font-size:13px">{warn}</ul>' if warn else
               '<p style="color:#4ade80;margin-top:24px;font-weight:700">Everything is configured.</p>')
            + '</body></html>')


@app.route('/admin/affiliates')
def admin_affiliates():
    """Who sent who, and what each shop is owed. Bookmark alongside /coach."""
    if not admin_authorised():
        return 'Not found', 404
    try:
        r = requests.get(f'{SUPABASE_URL}/rest/v1/ptrain_users?select=email,state,created_at',
                         headers=SUPABASE_HEADERS, timeout=20)
        users = r.json() if r.status_code == 200 else []
        stores = {}
        for u in users:
            st = u.get('state') or {}
            ref = (st.get('referredBy') or '').strip().lower()
            if not ref:
                continue
            s = stores.setdefault(ref, {'signups': 0, 'paying': 0})
            s['signups'] += 1
            if (st.get('userPlan') or 'free') != 'free':
                s['paying'] += 1

        MONTHLY = {'pro': 9.99, 'coach': 29.0, 'elite': 49.0}
        rows = ''
        total_owed = 0.0
        for name, s in sorted(stores.items(), key=lambda x: -x[1]['signups']):
            owed = s['paying'] * MONTHLY['pro'] * 0.30
            total_owed += owed
            rows += (f'<tr><td style="padding:11px 8px;color:#f5f5f0;font-weight:700">{name}</td>'
                     f'<td style="padding:11px 8px;text-align:center;color:#8b8b95">{s["signups"]}</td>'
                     f'<td style="padding:11px 8px;text-align:center;color:#8b8b95">{s["paying"]}</td>'
                     f'<td style="padding:11px 8px;text-align:right;color:#4ade80;font-weight:800">'
                     f'£{owed:.2f}</td></tr>')
        if not rows:
            rows = ('<tr><td colspan="4" style="padding:20px 8px;color:#5a5a64;text-align:center">'
                    'No referred signups yet.</td></tr>')

        return f"""<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<body style="background:#0a0a0d;color:#f5f5f0;font-family:-apple-system,sans-serif;padding:22px 16px 40px">
<div style="max-width:640px;margin:0 auto">
<div style="font-weight:900;font-size:22px;margin-bottom:3px">Affiliates</div>
<div style="color:#5a5a64;font-size:12.5px;margin-bottom:18px">30% of monthly, recurring · £{total_owed:.2f} owed this month</div>
<table style="width:100%;border-collapse:collapse;background:#16161b;border:1px solid #22222a;border-radius:14px;overflow:hidden">
<tr style="background:#1c1c22"><th style="padding:10px 8px;text-align:left;font-size:11px;color:#8b8b95;text-transform:uppercase">Store</th>
<th style="padding:10px 8px;font-size:11px;color:#8b8b95;text-transform:uppercase">Signups</th>
<th style="padding:10px 8px;font-size:11px;color:#8b8b95;text-transform:uppercase">Paying</th>
<th style="padding:10px 8px;text-align:right;font-size:11px;color:#8b8b95;text-transform:uppercase">Owed</th></tr>
{rows}</table>
<div style="color:#5a5a64;font-size:12px;margin-top:16px;line-height:1.6">
Give each shop its own link: ptrain.app/s/<b>storename</b><br>
Every scan from their card is stamped to them for good.</div></div></body>"""
    except Exception as e:
        return f'<pre>{e}</pre>', 500

@app.route('/admin/stripe')
def admin_stripe():
    """Creates every price and payment link the new pricing needs, in one go.
    Open once in Safari, then send me the links it prints.

    Nothing existing is deleted or changed — current subscribers keep paying
    exactly what they pay now. This only adds new prices alongside them."""
    if not admin_authorised():
        return 'Not found', 404
    if not STRIPE_SECRET_KEY:
        return 'STRIPE_SECRET_KEY not set', 500

    auth = {'Authorization': f'Bearer {STRIPE_SECRET_KEY}'}

    # name, pence, tier passed back on the redirect, description
    WANTED = [
        ('PTrain Pro',        999,  'pro',   'Unlimited plans, nutrition, Coach Chat'),
        ('PTrain Group Coaching', 2900, 'coach', 'Weekly group check-in with Tel + full app'),
        ('Coaching with Tel', 14900, 'elite', '1:1 coaching, weekly programming, direct access'),
    ]
    # one-off, not a subscription — for people who won't subscribe but will
    # pay once to have a real PT look at what the app built them
    ONE_OFF = [('Plan Review with Tel', 1900, 'review',
                'Tel reviews your plan and sends back a voice note within 48 hours')]

    out = []
    try:
        prods = requests.get('https://api.stripe.com/v1/products?active=true&limit=100',
                             headers=auth, timeout=20).json().get('data', [])
        for name, pence, tier, blurb in WANTED:
            product = next((p for p in prods
                            if (p.get('name') or '').strip().lower() == name.lower()), None)
            if not product:
                product = requests.post('https://api.stripe.com/v1/products', headers=auth,
                                        data={'name': name, 'description': blurb},
                                        timeout=20).json()
            price = requests.post('https://api.stripe.com/v1/prices', headers=auth, data={
                'product': product['id'],
                'unit_amount': pence,
                'currency': 'gbp',
                'recurring[interval]': 'month',
                'nickname': f'{name} £{pence/100:.2f}/mo'
            }, timeout=20).json()
            if price.get('error'):
                out.append((name, None, price['error'].get('message')))
                continue
            link = requests.post('https://api.stripe.com/v1/payment_links', headers=auth, data={
                'line_items[0][price]': price['id'],
                'line_items[0][quantity]': 1,
                'allow_promotion_codes': 'true',
                'after_completion[type]': 'redirect',
                'after_completion[redirect][url]':
                    f'https://ptrain.app/?success=true&plan={tier}&session_id={{CHECKOUT_SESSION_ID}}',
                'metadata[tier]': tier,
            }, timeout=20).json()
            if link.get('error'):
                out.append((name, None, link['error'].get('message')))
            else:
                out.append((name, link.get('url'), f'£{pence/100:.2f}/mo · {price["id"]}'))

        for name, pence, tier, blurb in ONE_OFF:
            product = next((p for p in prods
                            if (p.get('name') or '').strip().lower() == name.lower()), None)
            if not product:
                product = requests.post('https://api.stripe.com/v1/products', headers=auth,
                                        data={'name': name, 'description': blurb},
                                        timeout=20).json()
            price = requests.post('https://api.stripe.com/v1/prices', headers=auth, data={
                'product': product['id'], 'unit_amount': pence, 'currency': 'gbp',
                'nickname': f'{name} £{pence/100:.2f} one-off'
            }, timeout=20).json()
            if price.get('error'):
                out.append((name, None, price['error'].get('message')))
                continue
            link = requests.post('https://api.stripe.com/v1/payment_links', headers=auth, data={
                'line_items[0][price]': price['id'],
                'line_items[0][quantity]': 1,
                'after_completion[type]': 'redirect',
                'after_completion[redirect][url]':
                    'https://ptrain.app/?success=true&plan=review&session_id={CHECKOUT_SESSION_ID}',
                'metadata[tier]': 'review',
            }, timeout=20).json()
            if link.get('error'):
                out.append((name, None, link['error'].get('message')))
            else:
                out.append((name, link.get('url'), f'£{pence/100:.2f} one-off · {price["id"]}'))

        blocks = ''
        for name, url, note in out:
            if url:
                blocks += (f'<div style="background:#16161b;border:1px solid #22222a;border-radius:14px;'
                           f'padding:15px;margin-bottom:12px">'
                           f'<div style="color:#f5f5f0;font-weight:800;font-size:15px">{name}</div>'
                           f'<div style="color:#8b8b95;font-size:12px;margin:3px 0 9px">{note}</div>'
                           f'<code style="display:block;background:#0f0f13;border-radius:9px;padding:11px;'
                           f'color:#ff8a4f;word-break:break-all;font-size:12.5px">{url}</code></div>')
            else:
                blocks += (f'<div style="background:#2a1416;border:1px solid #4a1f24;border-radius:14px;'
                           f'padding:15px;margin-bottom:12px"><div style="color:#ff8a8a;font-weight:800">'
                           f'{name} failed</div><div style="color:#c99;font-size:12.5px">{note}</div></div>')

        return f"""<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<body style="background:#0a0a0d;color:#f5f5f0;font-family:-apple-system,sans-serif;padding:22px 16px 40px">
<div style="max-width:640px;margin:0 auto">
<div style="font-weight:900;font-size:22px;margin-bottom:4px">Stripe ready</div>
<div style="color:#5a5a64;font-size:12.5px;margin-bottom:18px">
Send these three links back and I'll wire them into the app.</div>
{blocks}
<div style="color:#5a5a64;font-size:12px;line-height:1.6;margin-top:14px">
Your existing £6.99 and £99 prices are untouched. Anyone already subscribed keeps their rate.</div>
</div></body>"""
    except Exception as e:
        return f'<pre>Error: {e}</pre>', 500

@app.route('/coach')
def coach_digest():
    """Tel's own dashboard. Bookmark it on the home screen.
    The machine watches everyone's data; this is where it hands over the
    handful of people worth a personal word from a human."""
    if not admin_authorised():
        return 'Not found', 404
    try:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_users?select=email,tier,state,updated_at',
            headers=SUPABASE_HEADERS, timeout=15
        )
        users = r.json() if r.status_code == 200 else []
        today = datetime.now(UK_TZ).date()

        at_risk, winning, fresh, stuck = [], [], [], []
        for u in users:
            st = u.get('state') or {}
            email = u.get('email') or 'unknown'
            hist = st.get('history') or []
            sessions = len(hist)
            gap = None
            if hist:
                try:
                    gap = (today - datetime.strptime(hist[0].get('date'), '%d/%m/%Y').date()).days
                except Exception:
                    gap = None
            pbs = len(st.get('pbs') or {})
            has_plan = bool((st.get('plan') or {}).get('days'))
            row = {'email': email, 'sessions': sessions, 'gap': gap, 'pbs': pbs,
                   'tier': st.get('userPlan') or u.get('tier') or 'free'}

            if has_plan and sessions == 0:
                stuck.append(row)                      # built a plan, never started
            elif gap is not None and 5 <= gap <= 14 and sessions >= 2:
                at_risk.append(row)                    # was training, slipping
            elif gap is not None and gap <= 4 and sessions >= 6:
                winning.append(row)                    # committed — coaching candidates
            elif not has_plan:
                fresh.append(row)                      # signed up, no plan

        def block(title, note, rows, colour):
            if not rows:
                return ''
            items = ''.join(
                f'<li style="padding:11px 0;border-top:1px solid #22222a">'
                f'<div style="color:#f5f5f0;font-weight:700;font-size:14px">{x["email"]}</div>'
                f'<div style="color:#8b8b95;font-size:12.5px;margin-top:2px">'
                f'{x["sessions"]} sessions'
                + (f' · {x["gap"]}d since last' if x["gap"] is not None else '')
                + (f' · {x["pbs"]} PBs' if x["pbs"] else '')
                + f' · {x["tier"]}</div></li>'
                for x in rows)
            return (f'<div style="background:#16161b;border:1px solid #22222a;border-left:3px solid {colour};'
                    f'border-radius:14px;padding:16px 18px;margin-bottom:14px">'
                    f'<div style="color:{colour};font-weight:800;font-size:12px;letter-spacing:.06em;'
                    f'text-transform:uppercase">{title} ({len(rows)})</div>'
                    f'<div style="color:#8b8b95;font-size:12.5px;margin:5px 0 2px">{note}</div>'
                    f'<ul style="list-style:none;padding:0;margin:6px 0 0">{items}</ul></div>')

        body = (
            block('Slipping', 'Were training, now 5-14 days quiet. A message from you lands hardest here.', at_risk, '#ff5a1f')
            + block('Worth a coaching offer', 'Training consistently. These are your £99 conversations.', winning, '#22c55e')
            + block('Never started', 'Built a plan, never logged a session. Ask what stopped them.', stuck, '#a855f7')
            + block('No plan yet', 'Signed up, never built anything.', fresh, '#3b82f6')
        ) or '<div style="color:#8b8b95;font-size:14px">Nobody needs you today. Everyone is either training or too new to tell.</div>'

        return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0a0a0d"><title>Coach digest</title></head>
<body style="margin:0;background:#0a0a0d;font-family:-apple-system,Inter,sans-serif;padding:20px 16px 40px">
<div style="max-width:640px;margin:0 auto">
<div style="color:#f5f5f0;font-weight:900;font-size:22px;margin-bottom:4px">Who needs you</div>
<div style="color:#5a5a64;font-size:12.5px;margin-bottom:18px">{today:%A %d %B} · {len(users)} users</div>
{body}</div></body></html>"""
    except Exception as e:
        return f'Error: {e}', 500

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

@app.route('/api/auth/request', methods=['POST', 'OPTIONS'])
def auth_request():
    """Email a sign-in link.

    Always answers the same way whether or not the address is known, so this
    cannot be used to find out who is a customer.
    """
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    if not origin_ok():
        return cors(jsonify({'error': 'Forbidden'})), 403
    try:
        email = str((request.get_json() or {}).get('email', '')).lower().strip()
        if not email or '@' not in email or len(email) > 200:
            return cors(jsonify({'error': 'Enter a valid email'})), 400
        if auth_request_limited(client_ip(), email):
            # same shape as success: no signal either way
            return cors(jsonify({'ok': True, 'sent': True}))
        token = issue_token(email)
        if token and SMTP_PASS:
            link = f'https://ptrain.app/?t={token}'
            try:
                _send_email(
                    email,
                    'Your PTrain sign-in link',
                    f"Here's your link back into PTrain:\n\n{link}\n\n"
                    f"It keeps you signed in on this device for {TOKEN_TTL_DAYS} days. "
                    f"If you didn't ask for it, ignore this email and nothing happens.\n\n"
                    f"Tel")
                print(f'[auth] sign-in link sent to {email}')
            except Exception as e:
                print(f'[auth] could not send link to {email}: {e}')
        elif token:
            print(f'[auth] token issued for {email} but no mailer configured')
        return cors(jsonify({'ok': True, 'sent': True}))
    except Exception as e:
        return cors(jsonify({'error': str(e)})), 500


@app.route('/api/auth/verify', methods=['POST', 'OPTIONS'])
def auth_verify():
    """Exchange a magic-link token for the address it belongs to."""
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    if not origin_ok():
        return cors(jsonify({'error': 'Forbidden'})), 403
    try:
        token = str((request.get_json() or {}).get('token', '')).strip()
        owner = email_for_token(token)
        if not owner:
            return cors(jsonify({'ok': False, 'error': 'That link has expired'})), 401
        return cors(jsonify({'ok': True, 'email': owner}))
    except Exception as e:
        return cors(jsonify({'error': str(e)})), 500


@app.route('/api/stripe/portal', methods=['POST', 'OPTIONS'])
def stripe_portal():
    """Open Stripe's billing portal so a subscriber can cancel or update their
    card themselves.

    The pricing page promises "cancel anytime" and until now there was no way
    to do it inside the app — people had to find an old receipt or email Tel.
    Stripe hosts the portal, so no card details ever touch this server.
    """
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    if not origin_ok():
        return cors(jsonify({'ok': False, 'error': 'Forbidden'})), 403
    if not STRIPE_SECRET_KEY:
        return cors(jsonify({'ok': False, 'error': 'STRIPE_SECRET_KEY not set'})), 500
    try:
        body = request.get_json() or {}
        email = str(body.get('email', '')).lower().strip()
        if not email or '@' not in email:
            return cors(jsonify({'ok': False, 'error': 'No email'})), 400
        # Only someone we already hold a paid tier for can open a portal, so
        # this can't be used to probe whether an arbitrary address is a customer.
        tier, _used = get_user_plan_state(email)
        if tier == 'free':
            return cors(jsonify({'ok': False, 'error': 'No active subscription'})), 404
        r = requests.get('https://api.stripe.com/v1/customers',
                         params={'email': email, 'limit': 1},
                         auth=(STRIPE_SECRET_KEY, ''), timeout=15)
        rows = (r.json().get('data') or []) if r.status_code == 200 else []
        if not rows:
            return cors(jsonify({'ok': False, 'error': 'No billing record for that email'})), 404
        s = requests.post('https://api.stripe.com/v1/billing_portal/sessions',
                          data={'customer': rows[0]['id'], 'return_url': 'https://ptrain.app/'},
                          auth=(STRIPE_SECRET_KEY, ''), timeout=15)
        if s.status_code != 200:
            msg = (s.json().get('error') or {}).get('message', f'HTTP {s.status_code}')
            print(f'[portal] Stripe refused a portal session for {email}: {msg}')
            return cors(jsonify({'ok': False, 'error': msg})), 502
        return cors(jsonify({'ok': True, 'url': s.json().get('url')}))
    except Exception as e:
        return cors(jsonify({'ok': False, 'error': str(e)})), 500


@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Stripe tells us when a subscription renews, lapses or is cancelled.

    Before this existed, a tier once granted was permanent: nothing revoked
    access when someone cancelled or their card failed, and anyone who paid but
    closed the tab before being redirected back never got their tier at all.

    Every request must carry a valid Stripe signature. Unrecognised events are
    acknowledged and ignored, so Stripe doesn't retry them forever.
    """
    payload = request.get_data()
    ok, err = verify_stripe_signature(payload, request.headers.get('Stripe-Signature', ''))
    if not ok:
        print(f'[webhook] rejected: {err}')
        # 400 so Stripe records the failure and retries a genuine delivery.
        return jsonify({'ok': False, 'error': 'Invalid signature'}), 400
    try:
        event = json.loads(payload)
    except Exception:
        return jsonify({'ok': False, 'error': 'Bad JSON'}), 400

    etype = event.get('type', '')
    obj = ((event.get('data') or {}).get('object') or {})
    print(f'[webhook] {etype} ({event.get("id", "no-id")})')

    try:
        # ---- someone finished paying ----------------------------------------
        # This is what makes a purchase stick even if they never come back to
        # the app: previously the only path that granted a tier was the browser
        # returning to ?success=true.
        if etype == 'checkout.session.completed':
            if obj.get('payment_status') != 'paid':
                return jsonify({'ok': True, 'ignored': 'not paid'})
            email = ((obj.get('customer_details') or {}).get('email') or '').lower().strip()
            session_id = obj.get('id', '')
            tier = None
            if session_id:
                full, serr = fetch_stripe_session(session_id)
                if serr:
                    print(f'[webhook] could not re-read session {session_id}: {serr}')
                    return jsonify({'ok': False, 'error': 'Session re-read failed'}), 502
                tier = tier_from_session(full)
                email = email or ((full.get('customer_details') or {}).get('email') or '').lower().strip()
            if not email:
                return jsonify({'ok': True, 'ignored': 'no email on session'})
            if tier == 'service':
                print(f'[webhook] one-off service purchase for {email}, tier unchanged')
                return jsonify({'ok': True, 'service': True})
            if not tier:
                print(f'[webhook] paid session {session_id} matched no known price')
                return jsonify({'ok': True, 'ignored': 'unrecognised price'})
            good, werr = apply_plan_upgrade(email, tier)
            if not good:
                return jsonify({'ok': False, 'error': werr}), 502
            print(f'[webhook] granted {tier} to {email}')
            return jsonify({'ok': True, 'plan': tier})

        # ---- subscription ended -------------------------------------------
        if etype == 'customer.subscription.deleted':
            email = email_for_stripe_customer(obj.get('customer'))
            if not email:
                return jsonify({'ok': True, 'ignored': 'no email for customer'})
            good, werr = apply_plan_downgrade(email, 'cancelled')
            return (jsonify({'ok': True}) if good
                    else (jsonify({'ok': False, 'error': werr}), 502))

        # ---- subscription changed: renewed, lapsed, or set to cancel ------
        if etype == 'customer.subscription.updated':
            status = obj.get('status', '')
            email = email_for_stripe_customer(obj.get('customer'))
            if not email:
                return jsonify({'ok': True, 'ignored': 'no email for customer'})
            if status in SUB_STATUS_ENDS_ACCESS:
                good, werr = apply_plan_downgrade(email, status)
                return (jsonify({'ok': True, 'downgraded': True}) if good
                        else (jsonify({'ok': False, 'error': werr}), 502))
            if status in ('active', 'trialing'):
                # A renewal, a plan change, or "cancel at period end" being set.
                # They keep access until the period actually ends, so re-assert
                # whatever the current price entitles them to.
                tier = tier_from_subscription(obj)
                if tier and tier != 'service':
                    good, werr = apply_plan_upgrade(email, tier)
                    if not good:
                        return jsonify({'ok': False, 'error': werr}), 502
                    return jsonify({'ok': True, 'plan': tier})
            return jsonify({'ok': True, 'status': status})

        # ---- payment failed -----------------------------------------------
        # Access is NOT pulled on the first failure. Stripe retries over
        # several days and most of these recover; the subscription moves to
        # unpaid or canceled if it doesn't, and that is handled above. Pulling
        # access here would lock out paying customers over a temporary decline.
        if etype in ('invoice.payment_failed', 'invoice.payment_action_required'):
            email = email_for_stripe_customer(obj.get('customer'))
            if email:
                st, read_err = _fetch_existing_state(email)
                if not read_err and st.get('userPlan', 'free') != 'free':
                    st['subStatus'] = 'payment_failed'
                    st['paymentFailedAt'] = datetime.now(timezone.utc).isoformat()
                    requests.post(
                        f'{SUPABASE_URL}/rest/v1/ptrain_users',
                        headers={**SUPABASE_HEADERS,
                                 'Prefer': 'resolution=merge-duplicates,return=minimal'},
                        json={'email': email, 'state': st, 'updated_at': 'now()'},
                        timeout=10)
                    print(f'[webhook] flagged payment failure for {email} (access kept)')
            return jsonify({'ok': True, 'flagged': True})

        # ---- payment succeeded: clear any failure flag ---------------------
        if etype in ('invoice.paid', 'invoice.payment_succeeded'):
            email = email_for_stripe_customer(obj.get('customer'))
            if email:
                st, read_err = _fetch_existing_state(email)
                if not read_err and st.get('subStatus') in ('payment_failed',):
                    st['subStatus'] = 'active'
                    st.pop('paymentFailedAt', None)
                    requests.post(
                        f'{SUPABASE_URL}/rest/v1/ptrain_users',
                        headers={**SUPABASE_HEADERS,
                                 'Prefer': 'resolution=merge-duplicates,return=minimal'},
                        json={'email': email, 'state': st, 'updated_at': 'now()'},
                        timeout=10)
            return jsonify({'ok': True})

        return jsonify({'ok': True, 'ignored': etype})
    except Exception as e:
        print(f'[webhook] handler error on {etype}: {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500


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
        data, stripe_err = fetch_stripe_session(session_id)
        # Stripe returned an error (wrong key, key/session mode mismatch, expired
        # session, etc.) — surface it instead of silently reporting paid:false.
        if stripe_err:
            print(f'[stripe_verify] Stripe API error for session {session_id}: {stripe_err}')
            return cors(jsonify({'ok': False, 'paid': False, 'error': stripe_err})), 502
        paid = data.get('payment_status') == 'paid'
        email = (data.get('customer_details') or {}).get('email', '')
        # The tier comes from what Stripe says was bought — never from the
        # browser. A ?plan= in the URL is now ignored entirely.
        tier = tier_from_session(data) if paid else None
        if paid and tier and email:
            ok, upgrade_err = apply_plan_upgrade(email, tier)
            if not ok:
                print(f'[stripe_verify] upgrade failed for {email}: {upgrade_err}')
        if paid and not tier:
            print(f'[stripe_verify] paid session {session_id} did not match any known price')
        return cors(jsonify({'ok': True, 'paid': paid, 'email': email, 'plan': tier}))
    except Exception as e:
        return cors(jsonify({'ok': False, 'paid': False, 'error': str(e)})), 500

# ─── SUPABASE USER STATE ───────────────────────────────────────────────────────

@app.route('/api/state/load', methods=['POST', 'OPTIONS'])
def load_state():
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    try:
        if not origin_ok():
            return cors(jsonify({'error': 'Forbidden'})), 403
        body_email = request.get_json().get('email', '').lower().strip()
        email, deny = authorised_email(body_email)
        if deny:
            return deny
        if not email:
            return cors(jsonify({'error': 'No email'})), 400
        if state_probe_blocked(client_ip(), email):
            print(f'[state] enumeration blocked from {client_ip()}')
            return cors(jsonify({'error': 'Too many requests'})), 429
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/ptrain_users?email=eq.{email}&select=*',
            headers=SUPABASE_HEADERS, timeout=10
        )
        data = r.json()
        if data and len(data) > 0:
            # The tier column is the only trustworthy record of what someone has
            # paid for: the Stripe webhook writes it, the client cannot. It used
            # to be withheld here, so the browser fell back to state.userPlan —
            # a value the client itself had written. A customer whose payment
            # landed, or whose tier was set by hand, stayed locked out of the
            # tier they owned until they happened to clear their device.
            return cors(jsonify({
                'found': True,
                'state': data[0].get('state', {}),
                'plan': data[0].get('plan'),
                'tier': data[0].get('tier') or 'free',
            }))
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
        if not origin_ok():
            return cors(jsonify({'error': 'Forbidden'})), 403
        body = request.get_json()
        body_email = body.get('email', '').lower().strip()
        email, deny = authorised_email(body_email)
        if deny:
            return deny
        if not email:
            return cors(jsonify({'error': 'No email'})), 400
        if state_probe_blocked(client_ip(), email):
            print(f'[state] enumeration blocked from {client_ip()}')
            return cors(jsonify({'error': 'Too many requests'})), 429
        state = body.get('state', {})
        plan = body.get('plan')
        # The plan goes in the `plan` column. It was previously written to
        # `tier` — the subscription column — which corrupted a paying user's
        # tier the moment they generated a plan, and meant /api/state/load
        # (which reads `plan`) always came back empty, so nobody's plan ever
        # survived a cleared browser or a change of phone.
        row = {'email': email, 'state': state, 'updated_at': 'now()'}
        if plan is not None:
            row['plan'] = plan
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/ptrain_users',
            headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
            json=row,
            timeout=10
        )
        if r.status_code >= 300:
            print(f'[save_state] Supabase write failed for {email}: {r.status_code} {r.text[:300]}')
            return cors(jsonify({'ok': False, 'error': f'Supabase write failed: {r.status_code}'})), 502
        return cors(jsonify({'ok': True, 'status': r.status_code}))
    except Exception as e:
        return cors(jsonify({'error': str(e)})), 500

@app.route('/api/state/upgrade', methods=['POST', 'OPTIONS'])
def upgrade_plan():
    """Apply a plan upgrade. Requires a paid Stripe session — the tier is read
    from that session, so a client can no longer name its own plan. Previously
    this endpoint trusted whatever the browser posted, which meant anyone could
    grant themselves Elite with a single request."""
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    try:
        if not STRIPE_SECRET_KEY:
            return cors(jsonify({'ok': False, 'error': 'STRIPE_SECRET_KEY not set'})), 500
        body = request.get_json() or {}
        email = str(body.get('email', '')).lower().strip()
        session_id = str(body.get('session_id', '')).strip()
        if not email:
            return cors(jsonify({'error': 'No email'})), 400
        if not session_id.startswith('cs_'):
            return cors(jsonify({'ok': False, 'error': 'A valid Stripe session is required'})), 400
        session, stripe_err = fetch_stripe_session(session_id)
        if stripe_err:
            return cors(jsonify({'ok': False, 'error': stripe_err})), 502
        if session.get('payment_status') != 'paid':
            return cors(jsonify({'ok': False, 'error': 'Session is not paid'})), 402
        tier = tier_from_session(session)
        if tier == 'service':
            # £19 plan review and similar one-offs: real purchases that grant no
            # subscription tier. Acknowledge them; never downgrade what they have.
            print(f'[upgrade_plan] one-off service purchase for {email}, tier unchanged')
            return cors(jsonify({'ok': True, 'service': True, 'plan': None}))
        if not tier:
            print(f'[upgrade_plan] unrecognised purchase on session {session_id}')
            return cors(jsonify({'ok': False, 'error': 'Purchase not recognised'})), 400
        # Make sure the session belongs to this person before granting anything.
        session_email = ((session.get('customer_details') or {}).get('email') or '').lower().strip()
        if session_email and session_email != email:
            print(f'[upgrade_plan] email mismatch: session {session_email} vs requested {email}')
            return cors(jsonify({'ok': False, 'error': 'Session does not match this account'})), 403
        ok, err = apply_plan_upgrade(email, tier)
        if not ok:
            return cors(jsonify({'ok': False, 'error': err})), 502
        return cors(jsonify({'ok': True, 'plan': tier}))
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
        # Which video or card sent them, if the link carried ?v=<slug>.
        # Logged so the source shows up next to the signup in the Render logs
        # even before the first plan save writes it into their state row.
        src = str(body.get('src', ''))[:40]
        if not email:
            return cors(jsonify({'error': 'No email'})), 400
        if src:
            print(f'[subscribe] {email} came from: {src}')

        r = requests.post(
            f'https://api.emailoctopus.com/lists/{EO_LIST_ID}/contacts',
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
                            # Level sets the base step, equipment sets what can
                            # actually be loaded. A flat step meant a lateral
                            # raise was told to jump the same as a back squat.
                            level = p.get('level', 'Intermediate')
                            ex_l = str(exercise).lower()
                            if re.search(r'sled|prowler', ex_l):
                                step = 5.0
                            elif re.search(r'kettlebell|\bkb\b', ex_l):
                                step = 4.0
                            elif re.search(r'cable|pulldown|pushdown|pec ?deck|leg press|leg curl|leg extension|seated row|machine|stack', ex_l):
                                step = 5.0
                            elif re.search(r'dumbbell|\bdb\b|goblet|lateral raise|hammer curl|concentration|preacher|carry|wall ball|med ?ball', ex_l):
                                step = 2.0
                            else:
                                step = 1.25 if level == 'Beginner' else 2.5
                            # Never propose a jump that would blow up a light
                            # lift; beginners get more room because fast linear
                            # progression is correct for them.
                            ceiling = 0.20 if level == 'Beginner' else 0.10
                            if step / last_kg > ceiling:
                                suggested = last_kg
                            else:
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

    return f"""You are PTrain, an AI personal trainer and nutrition coach. Built on real PT expertise by Tel Kershaw, certified PT, 8 years at Gymbox. Coach everyone — any age, level, goal, background. Explain WHY behind every decision.

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
- SESSION BLOCK STRUCTURE: every exercise must have a "block" field — one of "Strength", "Cardio", or "Mobility". A real session isn't one undifferentiated list — it has phases. Order exercises within the exercises array so all "Strength" entries come first, then "Cardio" (conditioning, intervals, circuit/WOD work), then "Mobility" (stretches, activation, corrective work) — grouped together by block, not interleaved randomly. Not every day needs all three blocks (a pure strength day can be 100% "Strength"), but when a day genuinely includes more than one type of work (e.g. lifting + a conditioning finisher), tag each exercise correctly so the phases are clearly separated, not blended into one list.
- FIRST PLAN (no lift history provided): prescribe CONSERVATIVE starting weights scaled to bodyweight and level. Put RPE guidance ("adjust so the last 2 reps feel hard, RPE 7-8") ONLY in the notes field.
- RAMP-UP SETS RULE (safety and credibility — this is the single clearest marker of a plan written by a real coach): the "sets" and "weight" fields describe WORKING sets only. Nobody goes from a general warm-up of bike and glute bridges straight into a near-maximal working set, and a plan that does that is both amateur and an injury risk. For every barbell or heavy compound lift prescribed at 8 reps or fewer, include a "warmupSets" field giving a concrete ramp in real loads and reps that builds to the first working set — for example, for a 110kg working weight: "Bar x 8, 50kg x 5, 75kg x 3, 95kg x 1". Derive it from the prescribed working weight at roughly 40-50%, 60-70%, 80-85%, then 90-95% for a single. Isolation work, machines, cardio, circuit stations and mobility must OMIT the warmupSets field entirely rather than carrying an empty string.
- WORKING SET VOLUME RULE: cap working sets on any one heavy compound at 3-4 when the rep range tops out at 6 or fewer. Five sets of 4-6 reps at RPE 8-9 on a single lift is more fatigue than stimulus, especially in a week that also contains conditioning or race-simulation days. Put additional volume into a second, lighter movement instead of stacking sets onto the first.
- TOP SET AND BACK-OFF RULE: never prescribe the identical load and the identical rep target on every set of a heavy lift. Either give a top set followed by back-off sets at a reduced load — state it plainly in notes (e.g. "Set 1 is your top set at this weight; drop to the back-off load for the rest and chase reps") AND set the numeric "backoff" field to the fraction of the top-set load the remaining sets use (0.9 for a 10% drop, 0.85 for 15%), so the app can show the real number in every set box instead of repeating the top set — or progress the rep target across the sets. Omit "backoff" entirely when every set genuinely uses the same load. The same number repeated down the page tells the athlete nothing about how to train the lift.
- CIRCUIT TAGGING: when movements form a circuit (see CIRCUIT/WOD STRUCTURE RULE), give every movement in that circuit the SAME "circuit" value (e.g. "A"), and set each one's "sets" to the number of rounds. Any movement that is not part of a circuit must omit the "circuit" field entirely. This is what lets the app show the work as rounds in the order it is actually trained, rather than every run, then every ski erg, then every set of wall balls.
- FOUR-WEEK BLOCK RULE: the days array is ONE week of sessions, and that week runs for four weeks. The "weekPlan" array says how each of those four weeks differs, so the user is not simply repeating an identical workout four times. Week 1 establishes working weights. Weeks 2 and 3 build — more reps within the range, then an extra set on the main lifts. Week 4 is a deload: fewer sets and around 20% off the load, so they arrive at the next block recovered rather than ground down. "setAdjust" is added to every exercise's set count that week (use -1, 0 or 1). "loadFactor" multiplies the weights that week (1 for normal weeks, about 0.8 for the deload). "intent" is one sentence written to the user explaining what that week is for, in a coach's voice. Adapt the shape to the training style: a strength block builds intensity while a conditioning or Hyrox block builds work capacity or pace, but every style gets a genuine four-week arc ending in a lighter week.
- CRITICAL FORMAT RULE: the "weight" field must be ONLY a number with kg (e.g. "40kg"), "BW" for genuine bodyweight moves (press-ups, pull-ups, Nordic curls), or "—" for pure cardio and time/distance work where there is no load at all (treadmill or track runs, ski erg, rower, bike). Never write "BW" against a run or an erg piece: the athlete is not lifting their bodyweight, there is simply nothing to load, and "BW" on a 1km run reads as a bug. NEVER words, sentences, ranges or explanations in the weight field — those belong in notes. A weight field containing text breaks the app.
- WITH lift history: use their actual numbers plus the progression step. Real data beats estimates.
- Diet preference is a HARD constraint: Keto = very low carb meals only, Vegan = no animal products, etc. Never include foods that violate the stated preference.
- REST PERIODS: give a restSeconds value on every exercise that matches the goal — roughly 150-210s for heavy strength work, 75-120s for hypertrophy, 45-75s for fat loss or conditioning, 30-60s for circuits. The app runs a rest timer from these.
- TRAINING TIME / MEAL TIMING: the profile includes when they train. Total calories and protein are what drive results, so never imply timing outweighs them — but DO lay the day out so the plan is actually followable around their session. Early morning: they will not eat a full breakfast at 5:30am, so give a small easily-digested carb-forward pre-session option (banana, toast and jam, small smoothie) and put the substantial protein and fibre into the meals AFTER training. Midday: normal breakfast, lighter lunch timed 1-2 hours before, main protein at dinner. After work / Evening: full breakfast and lunch, a carb-forward snack 1-2 hours before the session, and a dinner that lands within a couple of hours of finishing rather than very late. In every case name WHEN each meal is eaten relative to their session in the meal "foods" text (e.g. "45 min before training", "within 2 hrs after"). Keep one nutritionTips entry about their specific timing.
- THE FIRST SESSION DECIDES EVERYTHING. Most people who quit an app quit before they finish one workout, and a beginner who opens Day 1 and sees forty-five minutes of work they do not recognise will close it and not come back. So if Level is Beginner, DAY 1 SPECIFICALLY must be short and winnable: no more than FOUR exercises, 15-20 minutes of actual work, every movement a machine or a simple free-weight pattern they can picture. Not a watered-down programme — a deliberate first rung. Days 2 onward build to the full session length they asked for. Say so in the day's notes, in plain words: something like "Short one to start. You are learning the room and the movements, not testing yourself." Do NOT do this for Intermediate or Advanced — they will read it as the plan being soft.
- STARTING WEIGHTS FOR BEGINNERS: a beginner has no training history, so any number you print is a guess and a wrong one can hurt them or embarrass them. For Beginner level, pick loads that are clearly conservative, and in the exercise notes tell them the number is a starting point to be tested, not a target — e.g. "Start here. If you finish all sets with three or more reps still in you, add weight next session." Frame the first week as finding their numbers. Never imply a beginner should be hitting a specific weight.
- APPETITE (read this BEFORE calculating nutrition): the profile has an Appetite field. If it is "Low appetite" or "Very low appetite", the whole nutrition approach INVERTS and you must build it differently. Their problem is not restraint, it is physically getting enough in. Standard advice — bigger meals, eat more, volume eating, filling high-fibre foods — makes it worse and they will not follow it.
  * Protein first, every meal. Say so explicitly in the plan: eat the protein before anything else on the plate, because they will fill up before they finish.
  * Small and frequent beats three big meals. Use 5-6 smaller eating occasions rather than Breakfast/Lunch/Dinner/Snacks, and name them that way in the meals array.
  * Energy-dense, low-volume. Fortify what they do eat instead of adding more of it: olive oil, nut butter, full-fat dairy, protein powder stirred into things. Avoid bulky low-calorie foods and large salads — those fill them up for almost nothing.
  * Liquid and soft options matter. Smoothies, Greek yoghurt, soups, milk-based drinks, overnight oats. These go down when a plate of chicken and rice will not.
  * Keep protein target at the normal calculated level even if total calories land lower. Protein is the one thing that cannot slip — it is what protects their muscle. If total calories must come down, take it out of carbs and fats, never protein.
  * Hydration gets its own nutritionTips entry — appetite loss usually comes with drinking less, and dehydration makes the fatigue worse.
  * Tone: never congratulate them on eating less, never use restriction or willpower language, never imply a low appetite is an advantage for fat loss. It is a problem to be managed, and the risk is losing muscle, not failing to lose weight.
- APPETITE AND TRAINING: if appetite is "Low appetite" or "Very low appetite", also adapt the training. They are under-fuelled, so keep resistance training in (it is the thing protecting their lean mass) but reduce total session volume by roughly a third, keep the load and intensity on the main compound lifts, cut accessory and finisher work first, and shorten sessions. Prioritise two to three hard compound movements done properly over a long session they will not recover from. Put one line in the plan overview explaining why the volume is lower, so it does not read as an easy programme.
- NUTRITION CALCULATION: use Sex alongside Age/Weight/Height/activity level to calculate accurate BMR/TDEE and set dailyCalories/protein/carbs/fats — men and women have genuinely different baseline metabolic rates at identical stats, so this matters for accuracy. If Sex is "Prefer not to say", use a sensible midpoint estimate between typical male/female BMR for their stats — never mention the missing data, never ask about it again, just quietly produce a reasonable estimate.
- DUAL GOAL BLENDING: the Goal field may contain two goals joined by " + " (e.g. "Lose Fat + Increase Strength") — the person picked both intentionally, don't treat this as confusing or default to just one. Blend them into ONE coherent programme, don't alternate between two separate rule sets day to day. The most common real combination is fat loss + strength: this means genuine strength-focused resistance training (real progressive overload, not just circuits) combined with a calorie deficit through nutrition — i.e. body recomposition, which is standard, well-established programming, not a contradiction. For any two-goal combination, identify what training actually serves both simultaneously rather than compromising on either.
- Always match equipment strictly. Always respect injuries. Nutrition must match goal.

Profile: Name={p.get('name','User')}, Age={p.get('age','?')}, Sex={p.get('sex','Prefer not to say')}, Weight={p.get('weight','?')}kg, Height={p.get('height','?')}cm
Goal: {p.get('goal','General Fitness')} | Level: {p.get('level','Intermediate')} | Style: {p.get('style','Gym / Weights')}{' | Sport: ' + p.get('sport') if p.get('sport') else ''} | Days: {p.get('days','4')}/week | Duration: {p.get('duration','60')}min
Equipment: {p.get('equipment','Full Gym')} | Focus: {p.get('muscles','Full Body')}
Sleep: {p.get('sleep','7')}hrs | Stress: {p.get('stress','Moderate')} | Diet: {p.get('diet','No preference')} | Appetite: {p.get('appetite','Normal')} | Vibe: {p.get('vibe','Normal')}\nTrains: {p.get('trainTime','Not specified')}
Injuries: {p.get('inj','None')} | Allergies: {p.get('allergies','None')}
{progression_context}
CRITICAL: Respond ONLY with valid complete JSON. Be concise — short exercise notes, brief meal descriptions. No markdown, no backticks, no trailing commas:
{{"planName":"string","coachMessage":"1 sentence to {p.get('name','you')} on why this plan is their next step","overview":"2 sentences","weeklyVolume":0,"estimatedCalories":0,"days":[{{"day":"Day 1 - Focus","focus":"string","warmup":"string","exercises":[{{"name":"string","block":"Strength","sets":3,"reps":"8-10","weight":"70kg","rest":"90s","warmupSets":"Bar x 8, 40kg x 5, 55kg x 3","backoff":0.9,"notes":"brief why"}}],"cooldown":"string"}}],"nutritionPlan":{{"dailyCalories":0,"protein":0,"carbs":0,"fats":0,"meals":[{{"meal":"Breakfast","foods":"brief","calories":0}},{{"meal":"Lunch","foods":"brief","calories":0}},{{"meal":"Dinner","foods":"brief","calories":0}},{{"meal":"Snacks","foods":"brief","calories":0}}]}},"nutritionTips":["tip1","tip2","tip3"],"progressionScheme":"brief","deloadRecommendation":"brief","weekPlan":[{{"week":1,"focus":"2-3 words","intent":"1 sentence to the user","setAdjust":0,"loadFactor":1}},{{"week":2,"focus":"2-3 words","intent":"1 sentence","setAdjust":0,"loadFactor":1}},{{"week":3,"focus":"2-3 words","intent":"1 sentence","setAdjust":1,"loadFactor":1}},{{"week":4,"focus":"Deload","intent":"1 sentence","setAdjust":-1,"loadFactor":0.8}}]}}"""


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
            # ONE free refinement per person. The app asks them for the detail
            # they skipped, so the app pays for the rebuild — charging a free
            # user their only plan for answering our own question would be a
            # con, and the in-app copy promises it is free.
            if body.get('refine') and not _refine_already_used(email):
                _mark_refine_used(email, current_plan, plans_used)
            else:
                increment_plans_used(email, current_plan, plans_used)
        return cors(jsonify(result))
    except Exception as e:
        return cors(jsonify({'error': {'message': str(e)}})), 500

# Render runs this under gunicorn, which never executes __main__ — so the
# background workers are started on import instead, guarded so they only
# ever start once.
_workers_started = False
def _start_workers():
    global _workers_started
    if _workers_started:
        return
    _workers_started = True
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=reminder_worker, daemon=True).start()
    threading.Thread(target=_email_worker, daemon=True).start()
    threading.Thread(target=_weekly_digest_worker, daemon=True).start()
    threading.Thread(target=_activation_worker, daemon=True).start()

_start_workers()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), threaded=True)

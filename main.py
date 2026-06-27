from flask import Flask, request, jsonify, send_from_directory
import requests
import os
import threading
import time
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
RENDER_URL = os.environ.get('RENDER_EXTERNAL_URL', 'https://ptrain.onrender.com')
SUPABASE_URL = 'https://ysztaecwyjuqchmcgxjr.supabase.co'
SUPABASE_KEY = 'sb_publishable_2Y6Azzpv7SNDVRMng3QU7Q_bhiWuV2i'
EO_API_KEY = 'eo_347e5fd056d045949689c03e7b6ab07ea37140c549188ffb56bded816c850462'
EO_LIST_ID = 'badf6dd4-6fa7-11f1-bb46-e1bea4e2db03'

SUPABASE_HEADERS = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json',
    'Prefer': 'return=representation'
}

def cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.route('/')
def home():
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/manifest.json')
def manifest():
    return send_from_directory(BASE_DIR, 'manifest.json')

@app.route('/icon-192.png')
def icon192():
    return send_from_directory(BASE_DIR, 'icon-192.png')

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
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/ptrain_users',
            headers={**SUPABASE_HEADERS, 'Prefer': 'resolution=merge-duplicates,return=minimal'},
            json={'email': email, 'state': {'userPlan': plan_tier, 'plansUsed': 0}, 'updated_at': 'now()'},
            timeout=10
        )
        return cors(jsonify({'ok': True}))
    except Exception as e:
        return cors(jsonify({'error': str(e)})), 500

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

        requests.post(
            f'https://emailoctopus.com/api/2.0/lists/{EO_LIST_ID}/contacts',
            json={
                'api_key': EO_API_KEY,
                'email_address': email,
                'fields': {'FirstName': name},
                'status': 'SUBSCRIBED'
            },
            timeout=10
        )

        print(f'Welcome email queued for {email}')
        return cors(jsonify({'ok': True}))
    except Exception as e:
        return cors(jsonify({'error': str(e)})), 500

# ─── AI GENERATION ────────────────────────────────────────────────────────────

def build_prompt(profile):
    p = profile
    return f"""You are PTrAIn, an AI personal trainer and nutrition coach built on real certified PT expertise. You coach people of every age, body type, fitness level and health background. Explain the reasoning behind every key decision so users feel genuinely coached. Generate a complete {p.get('days','4')}-day training programme WITH a daily nutrition plan.

Profile: Name={p.get('name','User')}, Age={p.get('age','?')}, Weight={p.get('weight','?')}kg, Height={p.get('height','?')}cm
Goal: {p.get('goal','General Fitness')} | Level: {p.get('level','Intermediate')} | Days: {p.get('days','4')}/week | Duration: {p.get('duration','45')}min
Equipment: {p.get('equipment','Full Gym')} | Focus: {p.get('muscles','Full Body')}
Sleep: {p.get('sleep','7')}hrs/night | Stress: {p.get('stress','Moderate')} | Diet: {p.get('diet','No preference')} | Vibe: {p.get('vibe','Normal')}
Injuries: {p.get('inj','None')} | Food allergies: {p.get('allergies','None')}

CRITICAL: Respond ONLY with valid complete JSON. Keep text brief. No markdown, no backticks:
{{"planName":"string","coachMessage":"personal 1-sentence message to {p.get('name','you')} explaining WHY this plan suits them","overview":"2 sentences: what the plan does and WHY it suits their situation","weeklyVolume":0,"estimatedCalories":0,"days":[{{"day":"Day 1 - Focus","focus":"string","warmup":"string","exercises":[{{"name":"string","sets":3,"reps":"8-10","weight":"70kg","rest":"90s","notes":"WHY this exercise"}}],"cooldown":"string"}}],"nutritionPlan":{{"dailyCalories":0,"protein":0,"carbs":0,"fats":0,"meals":[{{"meal":"Breakfast","foods":"brief","calories":0}},{{"meal":"Lunch","foods":"brief","calories":0}},{{"meal":"Dinner","foods":"brief","calories":0}},{{"meal":"Snacks","foods":"brief","calories":0}}]}},"nutritionTips":["tip1","tip2","tip3"],"progressionScheme":"HOW and WHY to progress","deloadRecommendation":"brief"}}"""

@app.route('/api/generate', methods=['POST', 'OPTIONS'])
def generate():
    if request.method == 'OPTIONS':
        return cors(jsonify({'ok': True}))
    if not ANTHROPIC_API_KEY:
        return cors(jsonify({'error': {'message': 'ANTHROPIC_API_KEY not set'}})), 500
    try:
        body = request.get_json()
        if body.get('profile'):
            messages = [{'role': 'user', 'content': build_prompt(body['profile'])}]
            system = None
        else:
            messages = body.get('messages', [])
            system = body.get('system')
        payload = {
            'model': body.get('model', 'claude-sonnet-4-6'),
            'max_tokens': body.get('max_tokens', 8000),
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
            timeout=120,
        )
        return cors(jsonify(anthropic_response.json()))
    except Exception as e:
        return cors(jsonify({'error': {'message': str(e)}})), 500

if __name__ == '__main__':
    t = threading.Thread(target=keep_alive, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

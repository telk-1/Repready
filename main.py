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
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', 'sb_publishable_2Y6Azzpv7SNDVRMng3QU7Q_bhiWuV2i')
EO_API_KEY = os.environ.get('EO_API_KEY', '')
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

@app.route('/business')
def business():
    return send_from_directory(BASE_DIR, 'business.html')

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
    return send_from_directory(BASE_DIR, 'free-workout-plan.html')

@app.route('/ai-personal-trainer')
def ai_personal_trainer():
    return send_from_directory(BASE_DIR, 'ai-personal-trainer.html')

@app.route('/compare')
def compare():
    return send_from_directory(BASE_DIR, 'compare.html')

@app.route('/hyrox-training-plan')
def hyrox_training_plan():
    return send_from_directory(BASE_DIR, 'hyrox-training-plan.html')

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
        return cors(jsonify({'ok': True, 'status': r.status_code}))
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

    # Build progression context from user history
    progression_context = ''
    history = p.get('history', [])
    lift_history = p.get('liftHistory', {})
    pbs = p.get('pbs', {})
    session_ratings = p.get('sessionRatings', [])
    plans_completed = p.get('plansCompleted', 0)

    if history or lift_history:
        progression_context = '\n\nPROGRESSION DATA (use this to adapt the programme):\n'

        # Session count and ratings pattern
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

        # Recent PBs
        if pbs:
            pb_list = [f"{ex}: {data.get('weight','?')}" for ex, data in list(pbs.items())[:8] if isinstance(data, dict)]
            progression_context += f'Recent personal bests: {", ".join(pb_list)}\n'
            progression_context += 'INSTRUCTION: Acknowledge these PBs in the coachMessage and build on these strengths.\n'

        # Lift history for weight targets
        if lift_history:
            lift_context = []
            for exercise, entries in list(lift_history.items())[:10]:
                if entries:
                    latest = entries[-1]
                    raw_weight = str(latest.get('weight', 0)).replace('kg','').strip()
                    try:
                        last_kg = float(raw_weight) if raw_weight else None
                        if last_kg:
                            # Progression step based on level and weight
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
- Beginner: form focus, light weights, 1.25kg steps, technique notes
- Weight loss: cardio elements, deficit nutrition, HIIT finishers
- Rehabilitation: avoid contraindicated movements, mobility first
- No equipment: bodyweight only, tempo/volume progression
- Flexibility/Mobility: mobility sequences, longer holds
- Endurance: high reps, circuits, cardio bias, high carb nutrition
- Advanced/Elite: heavy compounds, 2.5kg steps, deload planning
- CrossFit style: WODs, AMRAPs, EMOMs, functional movements, time-based workouts
- Hyrox style: running intervals + functional fitness stations, sled, farmers carry, burpee broad jumps
- Calisthenics: bodyweight progressions, skill work, rings, handstands
- Sport Specific: build programme specifically for {p.get('sport','their sport')} — power, agility, speed, sport-specific conditioning and movement patterns
- HIIT/Circuit: short rest, high intensity, metabolic conditioning
- Always match equipment strictly. Always respect injuries. Nutrition must match goal.

Profile: Name={p.get('name','User')}, Age={p.get('age','?')}, Weight={p.get('weight','?')}kg, Height={p.get('height','?')}cm
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
        limit = request.args.get('limit', '1300')
        r = requests.get(
            f'https://exercisedb.p.rapidapi.com/exercises?limit={limit}&offset=0',
            headers={
                'x-rapidapi-host': 'exercisedb.p.rapidapi.com',
                'x-rapidapi-key': 'bef624a072msh78f618ba4adea63p1c4fa8jsnbdbc7cc47910'
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
            'max_tokens': body.get('max_tokens', 6000),
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
        return cors(jsonify(anthropic_response.json()))
    except Exception as e:
        return cors(jsonify({'error': {'message': str(e)}})), 500

if __name__ == '__main__':
    t = threading.Thread(target=keep_alive, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

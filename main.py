from flask import Flask, request, jsonify, send_from_directory
import requests
import os

app = Flask(__name__, static_folder='.', static_url_path='')

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')

@app.route('/')
def home():
    return send_from_directory('.', 'ptrain.html')

@app.route('/manifest.json')
def manifest():
    return send_from_directory('.', 'manifest.json')

@app.route('/icon-192.png')
def icon192():
    return send_from_directory('.', 'icon-192.png')

@app.route('/icon-512.png')
def icon512():
    return send_from_directory('.', 'icon-512.png')


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
        response = jsonify({'ok': True})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

    if not ANTHROPIC_API_KEY:
        return jsonify({'error': {'message': 'ANTHROPIC_API_KEY not set in Render Environment'}}), 500

    try:
        body = request.get_json()

        anthropic_response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
            },
            json={
                'model': body.get('model', 'claude-sonnet-4-6'),
                'max_tokens': body.get('max_tokens', 8000),
                'system': body.get('system'),
                'messages': [{'role':'user','content': build_prompt(body['profile']) if body.get('profile') else body.get('messages',[{}])[0].get('content','')}] if body.get('profile') else body.get('messages'),
            },
            timeout=120,
        )

        response = jsonify(anthropic_response.json())
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response

    except Exception as e:
        response = jsonify({'error': {'message': str(e)}})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response, 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

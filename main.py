from flask import Flask, request, jsonify, send_from_directory
import requests
import os
import sys

sys.stdout.reconfigure(line_buffering=u
app = Flask(__name__, static_folder='.', static_url_path='')

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY')

@app.route('/')
def home():
    return send_from_directory('.', 'ptrain.html')

@app.route('/api/create-checkout-session', methods=['POST', 'OPTIONS'])
def create_checkout_session():
    if request.method == 'OPTIONS':
        response = jsonify({'ok': True})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

    if not STRIPE_SECRET_KEY:
        return jsonify({'error': {'message': 'STRIPE_SECRET_KEY not set in Render Environment'}}), 500

    try:
        body = request.get_json()
        price_id = body.get('priceId')
        success_url = body.get('successUrl')
        cancel_url = body.get('cancelUrl')
        trial_days = body.get('trialDays')

        print(f"CHECKOUT DEBUG: price_id={price_id} success_url={success_url} cancel_url={cancel_url} trial_days={trial_days}", flush=True)

        form_data = {
            'mode': 'subscription',
            'line_items[0][price]': price_id,
            'line_items[0][quantity]': '1',
            'success_url': success_url,
            'cancel_url': cancel_url,
        }
        if trial_days:
            form_data['subscription_data[trial_period_days]'] = str(trial_days)

        stripe_response = requests.post(
            'https://api.stripe.com/v1/checkout/sessions',
            auth=(STRIPE_SECRET_KEY, ''),
            data=form_data,
            timeout=30,
        )
        data = stripe_response.json()

        print(f"CHECKOUT DEBUG: Stripe status={stripe_response.status_code} response={data}", flush=True)

        if stripe_response.status_code != 200:
            response = jsonify({'error': {'message': data.get('error', {}).get('message', 'Stripe error')}})
            response.headers['Access-Control-Allow-Origin'] = '*'
            return response, 400

        response = jsonify({'url': data.get('url')})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response

    except Exception as e:
        print(f"CHECKOUT DEBUG: Exception: {str(e)}")
        response = jsonify({'error': {'message': str(e)}})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response, 500

@app.route('/api/generate', methods=['POST', 'OPTIONS'])
def generate():
    if request.method == 'OPTIONS':
        # Handle CORS preflight
        response = jsonify({'ok': True})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

    if not ANTHROPIC_API_KEY:
        return jsonify({'error': {'message': 'ANTHROPIC_API_KEY not set in Replit Secrets'}}), 500

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
                'max_tokens': body.get('max_tokens', 4000),
                'messages': body.get('messages'),
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
